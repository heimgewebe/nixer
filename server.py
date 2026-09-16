from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

APP_NAME = "Nixer"
IDENTITY = "nixer-nix-specialist-v0"
DEFAULT_REPO_ROOT = Path.home() / "repos"
REPO_ROOT = Path(os.environ.get("NIXER_REPO_ROOT", str(DEFAULT_REPO_ROOT))).expanduser().resolve()
DOCKER_BIN = Path(os.environ.get("NIXER_DOCKER_BIN", "/usr/bin/docker"))
CONTAINER_PATH = os.environ.get("NIXER_CONTAINER_PATH", "/usr/bin:/bin")
CONTAINER_HOME = os.environ.get("NIXER_CONTAINER_HOME", os.environ.get("HOME", str(Path.home())))
CONTAINER_XDG_RUNTIME_DIR = os.environ.get(
    "NIXER_CONTAINER_XDG_RUNTIME_DIR", os.environ.get("XDG_RUNTIME_DIR", "")
)
NIX_BIN = "/nix/var/nix/profiles/default/bin/nix"
GIT_BIN = "/nix/var/nix/profiles/default/bin/git"
BASH_BIN = "/nix/var/nix/profiles/default/bin/bash"
SNAPSHOT_ROOT = "/tmp/nixer-workspace"
FLAKE_SOURCE = f"git+file://{SNAPSHOT_ROOT}"
SNAPSHOT_SCRIPT = r"""set -euo pipefail
umask 077
GIT=/nix/var/nix/profiles/default/bin/git
NIX=/nix/var/nix/profiles/default/bin/nix
SNAPSHOT=/tmp/nixer-workspace
PATCH=/tmp/nixer-worktree.patch
"$GIT" -c safe.directory=/workspace clone --no-local --no-hardlinks /workspace "$SNAPSHOT" >/dev/null
source_head="$("$GIT" -c safe.directory=/workspace -C /workspace rev-parse HEAD)"
snapshot_head="$("$GIT" -C "$SNAPSHOT" rev-parse HEAD)"
if [ "$source_head" != "$snapshot_head" ]; then
    printf 'Nixer snapshot HEAD mismatch\n' >&2
    exit 86
fi
"$GIT" -c safe.directory=/workspace -C /workspace diff --binary --no-ext-diff --no-textconv HEAD -- > "$PATCH"
if [ -s "$PATCH" ]; then
    "$GIT" -C "$SNAPSHOT" apply --whitespace=nowarn "$PATCH"
    "$GIT" -C "$SNAPSHOT" add -A -- .
fi
exec "$NIX" --extra-experimental-features "nix-command flakes" --option allow-import-from-derivation false "$@"
"""
PINNED_NIX_IMAGE_ID = "sha256:98edc6813218e179ce84587373e0b52d4aa58babae2d26b51fb01e7fdacf815f"
PINNED_NIX_IMAGE_TAG = "nixos/nix:2.35.2"
PINNED_NIX_IMAGE_REF = "nixos/nix@sha256:7a007c766426c1877758ddc5cb87a965ac131fc78c582ce0083d922d51ae945c"
MAX_OUTPUT_BYTES = 192_000
MAX_STDERR_BYTES = 64_000

READ_ANNOTATIONS = ToolAnnotations(
    title="Nix-only specialist evaluation",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

INSTRUCTIONS = """You are Nixer, a deliberately narrow specialist for Nix, Nixpkgs and NixOS.
Do not act as a general Linux, GitHub, Bureau, deployment or project-management operator.
Use the typed Nix tools to establish real evaluator evidence before recommending a workaround.
Never claim that a value, derivation or flake output was verified when the backend did not run
successfully. If the issue is not materially Nix-semantic, say so and hand it back to the general
operator. Nixer proposes Nix changes but never commits, pushes, merges, deploys, switches or
activates a system."""

mcp = FastMCP(APP_NAME, instructions=INSTRUCTIONS)

_ATTR_PATH_RE = re.compile(r"^[A-Za-z0-9_+\-]+(?:\.[A-Za-z0-9_+\-]+)*$")
_REPO_PART_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_IMAGE_ID_RE = re.compile(r"^(?:sha256:)?([0-9A-Fa-f]{64})$")
_SECRET_PATTERNS = (
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r'''(?im)(authorization|api[_-]?key|token|password|secret)\s*[:=]\s*(?:"(?:\\.|[^"\\\r\n])*"|\'(?:\\.|[^\'\\\r\n])*\'|[^\r\n,;]+)'''),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _redact(text: str) -> str:
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub("<REDACTED>", result)
    return result


def _bounded(text: str, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text, False
    clipped = encoded[:max_bytes].decode("utf-8", errors="replace")
    return clipped + "\n<OUTPUT_TRUNCATED>", True


def _run(argv: list[str], *, timeout: int = 120) -> dict[str, Any]:
    if not argv or argv[0] != str(DOCKER_BIN):
        raise ValueError("Nixer only executes the fixed container client")
    env = {
        "PATH": CONTAINER_PATH,
        "HOME": CONTAINER_HOME,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
    }
    if CONTAINER_XDG_RUNTIME_DIR:
        env["XDG_RUNTIME_DIR"] = CONTAINER_XDG_RUNTIME_DIR
    try:
        completed = subprocess.run(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return {
            "returncode": 127,
            "stdout": "",
            "stderr": "container client executable unavailable",
            "stdout_truncated": False,
            "stderr_truncated": False,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        stdout, stdout_truncated = _bounded(_redact(stdout), MAX_OUTPUT_BYTES)
        stderr, stderr_truncated = _bounded(_redact(stderr), MAX_STDERR_BYTES)
        return {
            "returncode": 124,
            "stdout": stdout,
            "stderr": stderr or f"operation exceeded {timeout}s timeout",
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "timed_out": True,
        }
    stdout, stdout_truncated = _bounded(_redact(completed.stdout), MAX_OUTPUT_BYTES)
    stderr, stderr_truncated = _bounded(_redact(completed.stderr), MAX_STDERR_BYTES)
    return {
        "returncode": completed.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "timed_out": False,
    }


def _resolve_repo(repo: str) -> Path:
    if not isinstance(repo, str) or not repo.strip():
        raise ValueError("repo must be a non-empty path or repository name")
    raw = Path(repo).expanduser()
    candidate = raw if raw.is_absolute() else REPO_ROOT / raw
    resolved = candidate.resolve(strict=True)
    try:
        relative = resolved.relative_to(REPO_ROOT)
    except ValueError as exc:
        raise PermissionError("repository is outside the configured repository root") from exc
    if not relative.parts or any(not _REPO_PART_RE.fullmatch(part) for part in relative.parts):
        raise PermissionError("repository path contains unsupported characters")
    if not resolved.is_dir() or not (resolved / ".git").exists():
        raise ValueError("repository must be a Git checkout under the configured repository root")
    return resolved


def _linked_git_common_dir(root: Path) -> Path | None:
    dot_git = root / ".git"
    if dot_git.is_dir():
        return None
    if not dot_git.is_file():
        raise ValueError("repository .git metadata must be a directory or linked-worktree file")

    raw = dot_git.read_text(encoding="utf-8")
    if len(raw.encode("utf-8")) > 4096:
        raise ValueError("linked-worktree .git metadata is too large")
    lines = raw.splitlines()
    if len(lines) != 1 or not lines[0].startswith("gitdir: "):
        raise ValueError("linked-worktree .git metadata is malformed")

    git_dir_value = lines[0][len("gitdir: "):]
    git_dir_candidate = Path(git_dir_value)
    if not git_dir_candidate.is_absolute():
        git_dir_candidate = dot_git.parent / git_dir_candidate
    git_dir = git_dir_candidate.resolve(strict=True)
    try:
        git_relative = git_dir.relative_to(REPO_ROOT)
    except ValueError as exc:
        raise PermissionError("linked-worktree Git directory is outside the repository root") from exc
    if any(not _REPO_PART_RE.fullmatch(part) for part in git_relative.parts):
        raise PermissionError("linked-worktree Git metadata path contains unsupported characters")

    commondir_file = git_dir / "commondir"
    if not commondir_file.is_file():
        raise ValueError("linked-worktree Git metadata has no commondir")
    commondir_raw = commondir_file.read_text(encoding="utf-8").strip()
    if not commondir_raw or len(commondir_raw.encode("utf-8")) > 4096:
        raise ValueError("linked-worktree commondir metadata is invalid")
    common_candidate = Path(commondir_raw)
    if not common_candidate.is_absolute():
        common_candidate = git_dir / common_candidate
    common_dir = common_candidate.resolve(strict=True)
    if not common_dir.is_dir():
        raise ValueError("linked-worktree common Git directory is unavailable")
    try:
        common_relative = common_dir.relative_to(REPO_ROOT)
    except ValueError as exc:
        raise PermissionError("linked-worktree common Git directory is outside the repository root") from exc
    if any(not _REPO_PART_RE.fullmatch(part) for part in common_relative.parts):
        raise PermissionError("linked-worktree Git metadata path contains unsupported characters")
    return common_dir


def _validate_attr_path(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not _ATTR_PATH_RE.fullmatch(value):
        raise ValueError(f"{field} must be a dot-separated Nix attribute path")
    if len(value) > 500:
        raise ValueError(f"{field} is too long")
    return value


def _canonical_image_id(value: str) -> str | None:
    match = _IMAGE_ID_RE.fullmatch(value.strip())
    if match is None:
        return None
    return f"sha256:{match.group(1).lower()}"


def _backend_probe() -> dict[str, Any]:
    if not DOCKER_BIN.is_absolute():
        return {
            "ready": False,
            "reason": "container_client_path_not_absolute",
            "container_client": str(DOCKER_BIN),
            "expected_image_id": PINNED_NIX_IMAGE_ID,
        }
    if not DOCKER_BIN.is_file():
        return {
            "ready": False,
            "reason": "container_client_unavailable",
            "container_client": str(DOCKER_BIN),
            "expected_image_id": PINNED_NIX_IMAGE_ID,
        }
    inspected = _run(
        [str(DOCKER_BIN), "image", "inspect", "--format", "{{.Id}}", PINNED_NIX_IMAGE_ID],
        timeout=15,
    )
    actual_raw = inspected["stdout"].strip() if inspected["returncode"] == 0 else ""
    actual = _canonical_image_id(actual_raw)
    if inspected["returncode"] != 0:
        return {
            "ready": False,
            "reason": "pinned_image_unavailable",
            "container_client": str(DOCKER_BIN),
            "expected_image_id": PINNED_NIX_IMAGE_ID,
            "image_tag_evidence": PINNED_NIX_IMAGE_TAG,
            "image_ref_evidence": PINNED_NIX_IMAGE_REF,
            "probe": inspected,
        }
    if actual != PINNED_NIX_IMAGE_ID:
        return {
            "ready": False,
            "reason": "pinned_image_identity_mismatch",
            "container_client": str(DOCKER_BIN),
            "expected_image_id": PINNED_NIX_IMAGE_ID,
            "actual_image_id": actual_raw,
            "probe": inspected,
        }
    return {
        "ready": True,
        "reason": "ready",
        "container_client": str(DOCKER_BIN),
        "image_id": actual,
        "image_tag_evidence": PINNED_NIX_IMAGE_TAG,
        "image_ref_evidence": PINNED_NIX_IMAGE_REF,
    }


def _docker_nix(repo: str, nix_args: list[str], *, operation: str, expect_json: bool = False) -> dict[str, Any]:
    root = _resolve_repo(repo)
    git_common_dir = _linked_git_common_dir(root)
    backend = _backend_probe()
    observed_at = _utc_now()
    if not backend["ready"]:
        return {
            "schema_version": 1,
            "identity": IDENTITY,
            "operation": operation,
            "repo": str(root),
            "ok": False,
            "status": "backend_unavailable",
            "backend": backend,
            "observed_at": observed_at,
        }

    mount = f"type=bind,src={root},dst=/workspace,readonly"
    argv = [
        str(DOCKER_BIN),
        "run",
        "--rm",
        "--pull=never",
        "--cap-drop=ALL",
        "--cap-add=CHOWN",
        "--cap-add=DAC_READ_SEARCH",
        "--security-opt=no-new-privileges",
        "--mount",
        mount,
    ]
    if git_common_dir is not None:
        git_metadata_mount = f"type=bind,src={git_common_dir},dst={git_common_dir},readonly"
        argv.extend(["--mount", git_metadata_mount])
    argv.extend([
        "--workdir=/workspace",
        "--entrypoint",
        BASH_BIN,
        PINNED_NIX_IMAGE_ID,
        "-lc",
        SNAPSHOT_SCRIPT,
        "nixer-snapshot",
        *nix_args,
    ])
    result = _run(argv)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "identity": IDENTITY,
        "operation": operation,
        "repo": str(root),
        "ok": result["returncode"] == 0,
        "status": "ok" if result["returncode"] == 0 else "nix_failed",
        "result": result,
        "backend": {
            "image_id": PINNED_NIX_IMAGE_ID,
            "container_client": str(DOCKER_BIN),
            "entrypoint": BASH_BIN,
            "nix_binary": NIX_BIN,
            "git_binary": GIT_BIN,
            "flake_source": FLAKE_SOURCE,
            "source_mode": "ephemeral-git-snapshot",
            "snapshot_semantics": "Git HEAD plus tracked working-tree diff; untracked files excluded",
            "linked_worktree_git_metadata": "read-only-common-dir" if git_common_dir else "in-worktree",
            "container_capabilities": ["CHOWN", "DAC_READ_SEARCH"],
            "repository_mount": "read-only",
            "container_persistence": "none (--rm)",
            "host_nix_required": False,
        },
        "observed_at": observed_at,
    }
    if expect_json and result["returncode"] == 0:
        try:
            payload["value"] = json.loads(result["stdout"])
        except json.JSONDecodeError as exc:
            payload["ok"] = False
            payload["status"] = "invalid_json_output"
            payload["json_error"] = str(exc)
    return payload


@mcp.tool(name="nixer_status", annotations=READ_ANNOTATIONS)
def nixer_status() -> dict[str, Any]:
    """Return Nixer's enforced scope and current pinned Nix backend readiness."""
    return {
        "schema_version": 1,
        "identity": IDENTITY,
        "service": APP_NAME,
        "mode": "nix-only-specialist",
        "scope": ["nix", "nixpkgs", "nixos"],
        "backend": _backend_probe(),
        "allowed_operations": [
            "flake_metadata",
            "flake_show",
            "flake_check_no_build",
            "eval_attr",
            "nixos_option",
            "derivation_show",
            "build_dry_run",
        ],
        "forbidden_effects": [
            "generic_shell",
            "repository_write",
            "git_commit",
            "git_push",
            "github_mutation",
            "merge",
            "bureau_mutation",
            "work_or_lease_acquisition",
            "real_nix_build",
            "nixos_rebuild",
            "nixos_switch_or_boot",
            "deploy",
            "service_control",
            "process_signal",
            "root_action",
            "secret_read_or_reveal",
        ],
        "observed_at": _utc_now(),
    }


@mcp.tool(name="flake_metadata", annotations=READ_ANNOTATIONS)
def flake_metadata(repo: str) -> dict[str, Any]:
    """Read real metadata for a repository flake without writing its lock file."""
    return _docker_nix(
        repo,
        ["flake", "metadata", "--json", "--no-write-lock-file", FLAKE_SOURCE],
        operation="flake_metadata",
        expect_json=True,
    )


@mcp.tool(name="flake_show", annotations=READ_ANNOTATIONS)
def flake_show(repo: str) -> dict[str, Any]:
    """Evaluate and return the output structure of a repository flake."""
    return _docker_nix(
        repo,
        ["flake", "show", "--json", "--no-write-lock-file", FLAKE_SOURCE],
        operation="flake_show",
        expect_json=True,
    )


@mcp.tool(name="flake_check", annotations=READ_ANNOTATIONS)
def flake_check(repo: str) -> dict[str, Any]:
    """Evaluate `nix flake check --no-build`; outputs are never realized."""
    return _docker_nix(
        repo,
        [
            "flake",
            "check",
            "--no-build",
            "--no-write-lock-file",
            "--show-trace",
            FLAKE_SOURCE,
        ],
        operation="flake_check_no_build",
    )


@mcp.tool(name="eval_attr", annotations=READ_ANNOTATIONS)
def eval_attr(repo: str, attribute: str) -> dict[str, Any]:
    """Evaluate one validated flake attribute as JSON."""
    attr = _validate_attr_path(attribute, field="attribute")
    installable = f"{FLAKE_SOURCE}#{attr}"
    result = _docker_nix(
        repo,
        ["eval", "--json", "--no-write-lock-file", installable],
        operation="eval_attr",
        expect_json=True,
    )
    result["attribute"] = attr
    return result


@mcp.tool(name="nixos_option", annotations=READ_ANNOTATIONS)
def nixos_option(repo: str, host: str, option: str) -> dict[str, Any]:
    """Evaluate one resulting NixOS config option for a named flake nixosConfiguration."""
    host_attr = _validate_attr_path(host, field="host")
    if "." in host_attr:
        raise ValueError("host must be a single Nix attribute segment")
    option_attr = _validate_attr_path(option, field="option")
    attribute = f"nixosConfigurations.{host_attr}.config.{option_attr}"
    installable = f"{FLAKE_SOURCE}#{attribute}"
    result = _docker_nix(
        repo,
        ["eval", "--json", "--no-write-lock-file", installable],
        operation="nixos_option",
        expect_json=True,
    )
    result["host"] = host_attr
    result["option"] = option_attr
    result["attribute"] = attribute
    return result


@mcp.tool(name="derivation_show", annotations=READ_ANNOTATIONS)
def derivation_show(repo: str, attribute: str) -> dict[str, Any]:
    """Evaluate and display the derivation for one validated flake installable."""
    attr = _validate_attr_path(attribute, field="attribute")
    installable = f"{FLAKE_SOURCE}#{attr}"
    result = _docker_nix(
        repo,
        ["derivation", "show", "--no-write-lock-file", installable],
        operation="derivation_show",
        expect_json=True,
    )
    result["attribute"] = attr
    return result


@mcp.tool(name="build_dry_run", annotations=READ_ANNOTATIONS)
def build_dry_run(repo: str, attribute: str) -> dict[str, Any]:
    """Ask Nix what a build would realize, without building or linking the output."""
    attr = _validate_attr_path(attribute, field="attribute")
    installable = f"{FLAKE_SOURCE}#{attr}"
    result = _docker_nix(
        repo,
        ["build", "--dry-run", "--no-link", "--no-write-lock-file", installable],
        operation="build_dry_run",
    )
    result["attribute"] = attr
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Nixer Nix-only specialist MCP service.")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18187)
    args = parser.parse_args()
    if args.transport == "streamable-http":
        if args.host != "127.0.0.1":
            raise SystemExit("Nixer HTTP transport must bind to 127.0.0.1")
        if not 1024 <= args.port <= 65535:
            raise SystemExit("port must be between 1024 and 65535")
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.settings.stateless_http = True
        if hasattr(mcp.settings, "session_idle_timeout"):
            mcp.settings.session_idle_timeout = None
        if hasattr(mcp.settings, "max_sessions"):
            mcp.settings.max_sessions = None
        mcp.settings.log_level = "WARNING"
        mcp.streamable_http_app()
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
