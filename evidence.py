from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, TextIO

import server

EVIDENCE_IDENTITY = "nixer-evidence-v1"
MAX_STRUCTURED_STDOUT_BYTES = 512_000
MAX_FAILURE_DETAIL_BYTES = 32_000
MAX_LIVE_LOG_LINE_BYTES = 16_384
MAX_BOOTSPEC_BYTES = 65_536
_SAFE_BOOTSPEC_V1_FIELDS = ("label", "system", "kernel", "initrd", "init", "toplevel")

SYSTEM_BUILD_SCRIPT = r'''set -euo pipefail
umask 077
GIT=/nix/var/nix/profiles/default/bin/git
NIX=/nix/var/nix/profiles/default/bin/nix
SNAPSHOT=/tmp/nixer-workspace
PATCH=/tmp/nixer-worktree.patch
HOST="$1"
FLAKE="git+file://$SNAPSHOT"

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

if "$GIT" -C "$SNAPSHOT" diff-index --quiet HEAD --; then
    dirty=false
else
    dirty=true
fi

# Real builds run as root inside the already capability-reduced disposable
# container. Disabling Nix build-user switching avoids granting SETUID/SETGID.
nix_base=("$NIX" --extra-experimental-features "nix-command flakes" --option allow-import-from-derivation false --option build-users-group "")
installable="$FLAKE#nixosConfigurations.$HOST.config.system.build.toplevel"
system_path="$("${nix_base[@]}" build --no-link --print-out-paths --no-write-lock-file "$installable")"
case "$system_path" in
    /nix/store/*) ;;
    *) printf 'Nixer system-build returned an invalid output path\n' >&2; exit 87 ;;
esac

kernel_path="$("${nix_base[@]}" eval --raw --no-write-lock-file "$FLAKE#nixosConfigurations.$HOST.config.system.build.kernel.outPath")"
initrd_path="$("${nix_base[@]}" eval --raw --no-write-lock-file "$FLAKE#nixosConfigurations.$HOST.config.system.build.initialRamdisk.outPath")"

printf 'NIXER_SOURCE_HEAD\t%s\n' "$source_head"
printf 'NIXER_SOURCE_DIRTY\t%s\n' "$dirty"
printf 'NIXER_SYSTEM_PATH\t%s\n' "$system_path"
printf 'NIXER_KERNEL_PATH\t%s\n' "$kernel_path"
printf 'NIXER_INITRD_PATH\t%s\n' "$initrd_path"
printf 'NIXER_PATH_INFO_BEGIN\n'
"${nix_base[@]}" path-info -S --json "$system_path"
printf '\nNIXER_PATH_INFO_END\n'

if [ -r "$system_path/boot.json" ]; then
    bootspec_size="$(wc -c < "$system_path/boot.json")"
    printf 'NIXER_BOOTSPEC_PATH\t%s\n' "$system_path/boot.json"
    printf 'NIXER_BOOTSPEC_SIZE\t%s\n' "$bootspec_size"
    if [ "$bootspec_size" -le 65536 ]; then
        printf 'NIXER_BOOTSPEC_BEGIN\n'
        cat "$system_path/boot.json"
        printf '\nNIXER_BOOTSPEC_END\n'
    else
        printf 'NIXER_BOOTSPEC_OMITTED\tover_size_limit\n'
    fi
else
    printf 'NIXER_BOOTSPEC_PATH\t\n'
    printf 'NIXER_BOOTSPEC_SIZE\t\n'
fi
'''


def _container_env() -> dict[str, str]:
    env = {
        "PATH": server.CONTAINER_PATH,
        "HOME": server.CONTAINER_HOME,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
    }
    if server.CONTAINER_XDG_RUNTIME_DIR:
        env["XDG_RUNTIME_DIR"] = server.CONTAINER_XDG_RUNTIME_DIR
    return env


def _append_bounded(buffer: bytearray, chunk: bytes, limit: int, *, keep_tail: bool) -> bool:
    if keep_tail:
        truncated = len(buffer) + len(chunk) > limit
        buffer.extend(chunk)
        if len(buffer) > limit:
            del buffer[: len(buffer) - limit]
        return truncated
    remaining = max(0, limit - len(buffer))
    truncated = len(chunk) > remaining
    if remaining:
        buffer.extend(chunk[:remaining])
    return truncated


class _RedactingLineMirror:
    def __init__(self, mirror: TextIO) -> None:
        self.mirror = mirror
        self.pending = bytearray()
        self.private_key_block = False
        self.disabled = False

    @staticmethod
    def _private_key_marker(text: str, kind: str) -> bool:
        upper = text.upper()
        return f"-----{kind} " in upper and "PRIVATE KEY-----" in upper

    def _emit_line(self, raw: bytes) -> None:
        text = raw.decode("utf-8", errors="replace")
        begin = self._private_key_marker(text, "BEGIN")
        end = self._private_key_marker(text, "END")
        if self.private_key_block or begin:
            self.mirror.write("<REDACTED_PRIVATE_KEY_BLOCK>\n")
            self.private_key_block = (self.private_key_block or begin) and not end
        else:
            self.mirror.write(server._redact(text))
        self.mirror.flush()

    def feed(self, raw: bytes) -> None:
        if self.disabled:
            return
        for value in raw:
            if value == 0x0A:
                self._emit_line(bytes(self.pending) + b"\n")
                self.pending.clear()
                continue
            if len(self.pending) >= MAX_LIVE_LOG_LINE_BYTES:
                self.pending.clear()
                self.disabled = True
                self.mirror.write("<REDACTED_OVERSIZED_LOG_STREAM>\n")
                self.mirror.flush()
                return
            self.pending.append(value)

    def finish(self) -> None:
        if not self.disabled and self.pending:
            self._emit_line(bytes(self.pending))
            self.pending.clear()


def _pump(
    stream: Any,
    buffer: bytearray,
    limit: int,
    state: dict[str, bool],
    *,
    mirror: TextIO | None,
    keep_tail: bool,
) -> None:
    redacting_mirror = _RedactingLineMirror(mirror) if mirror is not None else None
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            raw = chunk.encode("utf-8", errors="replace") if isinstance(chunk, str) else bytes(chunk)
            state["truncated"] |= _append_bounded(buffer, raw, limit, keep_tail=keep_tail)
            if redacting_mirror is not None:
                redacting_mirror.feed(raw)
        if redacting_mirror is not None:
            redacting_mirror.finish()
    finally:
        stream.close()


def _run_streaming(argv: list[str]) -> dict[str, Any]:
    if not argv or argv[0] != str(server.DOCKER_BIN):
        raise ValueError("Nixer evidence only executes the fixed container client")
    try:
        process = subprocess.Popen(
            argv,
            env=_container_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
    except FileNotFoundError:
        return {
            "returncode": 127,
            "stdout": "",
            "stderr": "container client executable unavailable",
            "stdout_truncated": False,
            "stderr_truncated": False,
        }

    assert process.stdout is not None
    assert process.stderr is not None
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    stdout_state = {"truncated": False}
    stderr_state = {"truncated": False}
    stdout_thread = threading.Thread(
        target=_pump,
        args=(
            process.stdout,
            stdout_buffer,
            MAX_STRUCTURED_STDOUT_BYTES,
            stdout_state,
        ),
        kwargs={"mirror": None, "keep_tail": False},
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_pump,
        args=(process.stderr, stderr_buffer, MAX_FAILURE_DETAIL_BYTES, stderr_state),
        kwargs={"mirror": sys.stderr, "keep_tail": True},
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    returncode = process.wait()
    stdout_thread.join()
    stderr_thread.join()

    stdout = stdout_buffer.decode("utf-8", errors="replace")
    stderr = server._redact(stderr_buffer.decode("utf-8", errors="replace"))
    return {
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": stdout_state["truncated"],
        "stderr_truncated": stderr_state["truncated"],
    }


def _system_build_argv(root: Path, git_common_dir: Path | None, host: str) -> list[str]:
    mount = f"type=bind,src={root},dst=/workspace,readonly"
    argv = [
        str(server.DOCKER_BIN),
        "run",
        "--rm",
        "--pull=never",
        "--cap-drop=ALL",
        "--cap-add=CHOWN",
        "--cap-add=DAC_READ_SEARCH",
        "--cap-add=FOWNER",
        "--security-opt=no-new-privileges",
        "--mount",
        mount,
    ]
    if git_common_dir is not None:
        argv.extend(
            [
                "--mount",
                f"type=bind,src={git_common_dir},dst={git_common_dir},readonly",
            ]
        )
    argv.extend(
        [
            "--workdir=/workspace",
            "--entrypoint",
            server.BASH_BIN,
            server.PINNED_NIX_IMAGE_ID,
            "-lc",
            SYSTEM_BUILD_SCRIPT,
            "nixer-system-build",
            host,
        ]
    )
    return argv


def _section(text: str, begin: str, end: str) -> str | None:
    start_marker = begin + "\n"
    end_marker = "\n" + end
    start = text.find(start_marker)
    if start < 0:
        return None
    start += len(start_marker)
    finish = text.find(end_marker, start)
    if finish < 0:
        return None
    return text[start:finish]


def _field(text: str, name: str) -> str | None:
    prefix = name + "\t"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    return None


def _parse_path_info(raw: str) -> tuple[int | None, Any]:
    value = json.loads(raw)
    records: list[dict[str, Any]] = []
    if isinstance(value, list):
        records = [entry for entry in value if isinstance(entry, dict)]
    elif isinstance(value, dict):
        if "path" in value:
            records = [value]
        else:
            records = [entry for entry in value.values() if isinstance(entry, dict)]
    closure_size = next(
        (
            entry.get("closureSize")
            for entry in records
            if isinstance(entry.get("closureSize"), int)
        ),
        None,
    )
    return closure_size, value


def _bootspec_summary(
    raw: str | None,
    path: str | None,
    *,
    size_bytes: int | None = None,
    omitted_reason: str | None = None,
) -> dict[str, Any]:
    if not path:
        return {"present": False, "path": None}
    result: dict[str, Any] = {"present": True, "path": path}
    if size_bytes is not None:
        result["size_bytes"] = size_bytes
    if omitted_reason is not None:
        result["summary_status"] = "omitted"
        result["omitted_reason"] = omitted_reason
        return result
    if raw is None:
        result["summary_status"] = "unavailable"
        return result
    value = json.loads(raw)
    if not isinstance(value, dict):
        return {**result, "summary_status": "ok", "format": "non-object-json"}
    result["summary_status"] = "ok"
    result["top_level_keys"] = sorted(str(key) for key in value)
    v1 = value.get("org.nixos.bootspec.v1")
    if isinstance(v1, dict):
        result["v1"] = {
            key: v1[key]
            for key in _SAFE_BOOTSPEC_V1_FIELDS
            if key in v1
            and (isinstance(v1[key], (str, int, float, bool)) or v1[key] is None)
        }
    return result


def _classify_failure(stderr: str, returncode: int) -> str:
    lowered = stderr.lower()
    if returncode == 127:
        return "container_client_unavailable"
    if "no space left on device" in lowered or "disk quota exceeded" in lowered:
        return "disk_exhausted"
    if "hash mismatch" in lowered:
        return "fixed_output_hash_mismatch"
    if "does not provide attribute" in lowered and "nixosconfigurations" in lowered:
        return "missing_nixos_configuration"
    if "a '" in lowered and "with features" in lowered and "is required" in lowered:
        return "builder_unavailable"
    if "builder for" in lowered and "failed" in lowered:
        return "derivation_failed"
    if "evaluation aborted" in lowered or "while evaluating" in lowered:
        return "evaluation_failed"
    return "nix_build_failed"


def _evidence_backend() -> dict[str, Any]:
    return {
        "image_id": server.PINNED_NIX_IMAGE_ID,
        "container_client": str(server.DOCKER_BIN),
        "source_mode": "ephemeral-git-snapshot",
        "snapshot_semantics": "Git HEAD plus tracked working-tree diff; untracked files excluded",
        "repository_mount": "read-only",
        "container_persistence": "none (--rm)",
        "build_users_group": "disabled",
        "lifecycle_owner": "external operator",
    }


def _evidence_failure(
    repo: Path,
    host: str,
    observed_at: str,
    failure_class: str,
    detail: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "identity": EVIDENCE_IDENTITY,
        "operation": "system-build",
        "repo": str(repo),
        "host": host,
        "success": False,
        "status": "evidence_failed",
        "backend": _evidence_backend(),
        "failure": {"class": failure_class, "detail": server._redact(detail)},
        "observed_at": observed_at,
    }


def system_build(repo: str, host: str) -> dict[str, Any]:
    root = server._resolve_repo(repo)
    host_attr = server._validate_attr_path(host, field="host")
    if "." in host_attr:
        raise ValueError("host must be a single Nix attribute segment")
    git_common_dir = server._linked_git_common_dir(root)
    backend = server._backend_probe()
    observed_at = server._utc_now()
    if not backend["ready"]:
        return {
            "schema_version": 1,
            "identity": EVIDENCE_IDENTITY,
            "operation": "system-build",
            "repo": str(root),
            "host": host_attr,
            "success": False,
            "status": "backend_unavailable",
            "backend": {**backend, "lifecycle_owner": "external operator"},
            "failure": {"class": "backend_unavailable", "detail": backend.get("reason")},
            "observed_at": observed_at,
        }

    result = _run_streaming(_system_build_argv(root, git_common_dir, host_attr))
    if result["returncode"] != 0:
        return {
            "schema_version": 1,
            "identity": EVIDENCE_IDENTITY,
            "operation": "system-build",
            "repo": str(root),
            "host": host_attr,
            "success": False,
            "status": "build_failed",
            "backend": _evidence_backend(),
            "failure": {
                "class": _classify_failure(result["stderr"], result["returncode"]),
                "nix_exit_code": result["returncode"],
                "detail": result["stderr"],
                "detail_truncated": result["stderr_truncated"],
            },
            "observed_at": observed_at,
        }

    if result["stdout_truncated"]:
        return _evidence_failure(
            root,
            host_attr,
            observed_at,
            "structured_output_too_large",
            "structured system-build output exceeded its hard bound",
        )
    stdout = result["stdout"]
    system_path = _field(stdout, "NIXER_SYSTEM_PATH")
    kernel_path = _field(stdout, "NIXER_KERNEL_PATH")
    initrd_path = _field(stdout, "NIXER_INITRD_PATH")
    source_head = _field(stdout, "NIXER_SOURCE_HEAD")
    source_dirty = _field(stdout, "NIXER_SOURCE_DIRTY")
    bootspec_path = _field(stdout, "NIXER_BOOTSPEC_PATH") or None
    bootspec_size_raw = _field(stdout, "NIXER_BOOTSPEC_SIZE") or None
    bootspec_omitted = _field(stdout, "NIXER_BOOTSPEC_OMITTED") or None
    path_info_raw = _section(stdout, "NIXER_PATH_INFO_BEGIN", "NIXER_PATH_INFO_END")
    bootspec_raw = _section(stdout, "NIXER_BOOTSPEC_BEGIN", "NIXER_BOOTSPEC_END")
    required = {
        "source_head": source_head,
        "source_dirty": source_dirty,
        "system_path": system_path,
        "kernel_path": kernel_path,
        "initrd_path": initrd_path,
        "path_info": path_info_raw,
    }
    missing = sorted(key for key, value in required.items() if value is None)
    if missing:
        return _evidence_failure(
            root,
            host_attr,
            observed_at,
            "structured_output_invalid",
            f"system-build output is missing structured fields: {', '.join(missing)}",
        )
    assert path_info_raw is not None
    try:
        bootspec_size = int(bootspec_size_raw) if bootspec_size_raw is not None else None
        if bootspec_size is not None and bootspec_size < 0:
            raise ValueError("negative boot specification size")
        closure_size, path_info = _parse_path_info(path_info_raw)
        bootspec = _bootspec_summary(
            bootspec_raw,
            bootspec_path,
            size_bytes=bootspec_size,
            omitted_reason=bootspec_omitted,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return _evidence_failure(
            root,
            host_attr,
            observed_at,
            "structured_output_invalid",
            f"could not parse structured system-build output: {exc}",
        )

    return {
        "schema_version": 1,
        "identity": EVIDENCE_IDENTITY,
        "operation": "system-build",
        "repo": str(root),
        "host": host_attr,
        "success": True,
        "status": "ok",
        "source_observation": {
            "git_head": source_head,
            "dirty": source_dirty == "true",
            "authority": "observation_only",
        },
        "system": {
            "output_path": system_path,
            "closure_size_bytes": closure_size,
            "path_info": path_info,
        },
        "boot": {
            "kernel": kernel_path,
            "initrd": initrd_path,
            "bootspec": bootspec,
        },
        "backend": _evidence_backend(),
        "observed_at": observed_at,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run fixed, Nix-domain evidence operations outside the Nixer MCP lifecycle."
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)
    system = subparsers.add_parser("system-build", help="Build one NixOS system toplevel")
    system.add_argument("--repo", required=True)
    system.add_argument("--host", required=True)
    args = parser.parse_args()

    try:
        payload = system_build(args.repo, args.host)
    except (ValueError, PermissionError, FileNotFoundError) as exc:
        payload = {
            "schema_version": 1,
            "identity": EVIDENCE_IDENTITY,
            "operation": args.operation,
            "success": False,
            "status": "invalid_input",
            "failure": {"class": "invalid_input", "detail": server._redact(str(exc))},
            "observed_at": server._utc_now(),
        }
    json.dump(payload, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    raise SystemExit(0 if payload.get("success") else 1)


if __name__ == "__main__":
    main()