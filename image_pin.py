from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import subprocess
import time
from pathlib import Path
from typing import Any

PINNED_NIX_IMAGE_ID = "sha256:98edc6813218e179ce84587373e0b52d4aa58babae2d26b51fb01e7fdacf815f"
PINNED_NIX_IMAGE_TAG = "nixos/nix:2.35.2"
PINNED_NIX_IMAGE_REF = "nixos/nix@sha256:7a007c766426c1877758ddc5cb87a965ac131fc78c582ce0083d922d51ae945c"

_IMAGE_ID_RE = re.compile(r"^(?:sha256:)?([0-9A-Fa-f]{64})$")
_MAX_DIAGNOSTIC_CHARS = 4096


def _canonical_image_id(value: str) -> str | None:
    match = _IMAGE_ID_RE.fullmatch(value.strip())
    if match is None:
        return None
    return f"sha256:{match.group(1).lower()}"


def _container_cli() -> Path:
    return Path(os.environ.get("NIXER_DOCKER_BIN", "/usr/bin/docker"))


def _container_env() -> dict[str, str]:
    env = {
        "PATH": os.environ.get("NIXER_CONTAINER_PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("NIXER_CONTAINER_HOME", os.environ.get("HOME", str(Path.home()))),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
    }
    runtime_dir = os.environ.get(
        "NIXER_CONTAINER_XDG_RUNTIME_DIR", os.environ.get("XDG_RUNTIME_DIR", "")
    )
    if runtime_dir:
        env["XDG_RUNTIME_DIR"] = runtime_dir
    return env


def _run(cli: Path, args: list[str], *, timeout: int = 300) -> dict[str, Any]:
    """Drain both client pipes while retaining only bounded diagnostic tails.

    Image inspection must reject oversized stdout instead of accepting an
    apparently valid ID from the tail of hostile or malformed output.
    """
    command = [str(cli), *args]
    tails: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"stdout": False, "stderr": False}
    try:
        process = subprocess.Popen(
            command,
            env=_container_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        return {"returncode": 127, "stdout": "", "stderr": "container client unavailable"}
    except OSError:
        return {"returncode": 126, "stdout": "", "stderr": "container client execution failed"}

    deadline = time.monotonic() + timeout
    status = 126
    try:
        with selectors.DefaultSelector() as selector:
            for name in ("stdout", "stderr"):
                pipe = getattr(process, name)
                if pipe is None:
                    raise OSError("container client pipe missing")
                selector.register(pipe, selectors.EVENT_READ, name)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    name = key.data
                    tail = tails[name]
                    if len(tail) + len(chunk) > _MAX_DIAGNOSTIC_CHARS:
                        truncated[name] = True
                    tail.extend(chunk)
                    if len(tail) > _MAX_DIAGNOSTIC_CHARS:
                        del tail[:-_MAX_DIAGNOSTIC_CHARS]
        status = process.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        status = 124
    except OSError:
        status = 126
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()

    stdout = tails["stdout"].decode("utf-8", "replace")[-_MAX_DIAGNOSTIC_CHARS:]
    stderr = tails["stderr"].decode("utf-8", "replace")[-_MAX_DIAGNOSTIC_CHARS:]
    if status == 124 and not stderr:
        stderr = f"operation exceeded {timeout}s timeout"
    elif status == 126 and not stderr:
        stderr = "container client output unavailable"
    # Reject noise-prefix attacks even if their trailing bytes look like an ID.
    if args[:2] == ["image", "inspect"] and truncated["stdout"]:
        return {
            "returncode": 125,
            "stdout": "",
            "stderr": "container image inspection output exceeded bound",
        }
    return {"returncode": status, "stdout": stdout, "stderr": stderr}


def _inspect_id(cli: Path, reference: str) -> tuple[str | None, dict[str, Any]]:
    probe = _run(cli, ["image", "inspect", "--format", "{{.Id}}", reference], timeout=30)
    if probe["returncode"] != 0:
        return None, probe
    return _canonical_image_id(probe["stdout"]), probe


def ensure_pinned_image(*, pull_missing: bool) -> dict[str, Any]:
    cli = _container_cli()
    base: dict[str, Any] = {
        "schema_version": 1,
        "kind": "nixer.pinned_image_provisioning",
        "container_client": str(cli),
        "expected_image_id": PINNED_NIX_IMAGE_ID,
        "image_ref": PINNED_NIX_IMAGE_REF,
        "retention_tag": PINNED_NIX_IMAGE_TAG,
    }
    if not cli.is_absolute():
        return {**base, "ok": False, "status": "container_client_path_not_absolute"}
    if not cli.is_file():
        return {**base, "ok": False, "status": "container_client_unavailable"}

    tag_id, tag_probe = _inspect_id(cli, PINNED_NIX_IMAGE_TAG)
    if tag_id is None:
        # A failed inspection is not proof that the tag is absent. In
        # particular, never overwrite a local tag after malformed ID output,
        # timeout, oversized stdout, or an inaccessible container client.
        diagnostic = tag_probe["stderr"].lower()
        missing = (
            tag_probe["returncode"] in {1, 125}
            and any(marker in diagnostic for marker in (
                "no such image", "image not known", "no image found"
            ))
        )
        if not missing:
            return {
                **base,
                "ok": False,
                "status": "retention_tag_probe_failed",
                "probe": tag_probe,
            }
    if tag_id is not None and tag_id != PINNED_NIX_IMAGE_ID:
        return {
            **base,
            "ok": False,
            "status": "retention_tag_conflict",
            "actual_tag_image_id": tag_id,
        }

    image_id, image_probe = _inspect_id(cli, PINNED_NIX_IMAGE_ID)
    pulled = False
    if image_id is None:
        if not pull_missing:
            return {
                **base,
                "ok": False,
                "status": "pinned_image_unavailable",
                "probe": image_probe,
            }
        pull = _run(cli, ["pull", PINNED_NIX_IMAGE_REF], timeout=900)
        if pull["returncode"] != 0:
            return {**base, "ok": False, "status": "immutable_pull_failed", "probe": pull}
        pulled = True
        image_id, image_probe = _inspect_id(cli, PINNED_NIX_IMAGE_ID)

    if image_id != PINNED_NIX_IMAGE_ID:
        return {
            **base,
            "ok": False,
            "status": "pinned_image_identity_mismatch",
            "actual_image_id": image_id,
            "probe": image_probe,
        }

    ref_id, _ = _inspect_id(cli, PINNED_NIX_IMAGE_REF)
    if ref_id is not None and ref_id != PINNED_NIX_IMAGE_ID:
        return {
            **base,
            "ok": False,
            "status": "immutable_ref_identity_mismatch",
            "actual_ref_image_id": ref_id,
        }

    tagged = False
    if tag_id is None:
        tag = _run(cli, ["tag", PINNED_NIX_IMAGE_ID, PINNED_NIX_IMAGE_TAG], timeout=30)
        if tag["returncode"] != 0:
            return {**base, "ok": False, "status": "retention_tag_failed", "probe": tag}
        tagged = True

    final_tag_id, final_tag_probe = _inspect_id(cli, PINNED_NIX_IMAGE_TAG)
    if final_tag_id != PINNED_NIX_IMAGE_ID:
        return {
            **base,
            "ok": False,
            "status": "retention_tag_identity_mismatch",
            "actual_tag_image_id": final_tag_id,
            "probe": final_tag_probe,
        }

    return {
        **base,
        "ok": True,
        "status": "ready",
        "image_id": image_id,
        "immutable_ref_local": ref_id == PINNED_NIX_IMAGE_ID,
        "pulled": pulled,
        "tagged": tagged,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Provision Nixer's exact pinned OCI image without resolving a mutable tag."
    )
    parser.add_argument(
        "--pull-missing",
        action="store_true",
        help="pull the exact immutable digest when the expected image ID is absent",
    )
    args = parser.parse_args()
    result = ensure_pinned_image(pull_missing=args.pull_missing)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())