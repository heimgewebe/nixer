from __future__ import annotations

import argparse
import json
import re
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
_SAFE_BOOTSPEC_V1_PATH_FIELDS = ("kernel", "initrd", "toplevel")
_STRUCTURED_STORE_PATH_RE = re.compile(r"^/nix/store/[^/\x00-\x20\x7f]+$")

_EVIDENCE_STAGE_FAILURE_RE = re.compile(
    r"(?:^|\n)NIXER_EVIDENCE_STAGE_FAILURE\t([a-z0-9_]+)\t([0-9]{1,3})(?=\n|$)"
)
_EVIDENCE_FAILURE_STAGES = frozenset(
    {
        "snapshot_clone",
        "source_head",
        "snapshot_head",
        "snapshot_diff",
        "snapshot_apply",
        "snapshot_index",
        "snapshot_dirty",
        "nix_build",
        "kernel_eval",
        "kernel_validate",
        "initrd_eval",
        "initrd_validate",
        "structured_output",
        "path_info",
        "bootspec_size",
        "bootspec_read",
    }
)

SYSTEM_BUILD_SCRIPT = r'''set -euo pipefail
umask 077
GIT=/nix/var/nix/profiles/default/bin/git
NIX=/nix/var/nix/profiles/default/bin/nix
SNAPSHOT=/tmp/nixer-workspace
PATCH=/tmp/nixer-worktree.patch
HOST="$1"
FLAKE="git+file://$SNAPSHOT"
stage=bootstrap

stage_fail_code() {
    failure_rc="$1"
    trap - ERR
    printf '\nNIXER_EVIDENCE_STAGE_FAILURE\t%s\t%s\n' "$stage" "$failure_rc" >&2
    exit 88
}

stage_fail() {
    failure_rc="$?"
    stage_fail_code "$failure_rc"
}
trap stage_fail ERR

stage=snapshot_clone
"$GIT" -c safe.directory=/workspace clone --no-local --no-hardlinks /workspace "$SNAPSHOT" >/dev/null
stage=source_head
source_head="$("$GIT" -c safe.directory=/workspace -C /workspace rev-parse HEAD)"
stage=snapshot_head
snapshot_head="$("$GIT" -C "$SNAPSHOT" rev-parse HEAD)"
if [ "$source_head" != "$snapshot_head" ]; then
    printf 'Nixer snapshot HEAD mismatch\n' >&2
    exit 86
fi
stage=snapshot_diff
"$GIT" -c safe.directory=/workspace -C /workspace diff --binary --no-ext-diff --no-textconv HEAD -- > "$PATCH"
if [ -s "$PATCH" ]; then
    stage=snapshot_apply
    "$GIT" -C "$SNAPSHOT" apply --whitespace=nowarn "$PATCH"
    stage=snapshot_index
    "$GIT" -C "$SNAPSHOT" add -A -- .
fi

stage=snapshot_dirty
if "$GIT" -C "$SNAPSHOT" diff-index --quiet HEAD --; then
    dirty=false
else
    dirty_rc="$?"
    case "$dirty_rc" in
        1) dirty=true ;;
        *) stage_fail_code "$dirty_rc" ;;
    esac
fi

# Real builds run as root inside the already capability-reduced disposable
# container. Disabling Nix build-user switching avoids granting SETUID/SETGID.
nix_base=("$NIX" --extra-experimental-features "nix-command flakes" --option allow-import-from-derivation false --option build-users-group "")
installable="$FLAKE#nixosConfigurations.$HOST.config.system.build.toplevel"
stage=nix_build
system_path="$("${nix_base[@]}" build --no-link --print-out-paths --no-write-lock-file "$installable")"
case "$system_path" in
    /nix/store/*) ;;
    *) printf 'Nixer system-build returned an invalid output path\n' >&2; exit 87 ;;
esac

stage=kernel_eval
kernel_path="$("${nix_base[@]}" eval --raw --no-write-lock-file "$FLAKE#nixosConfigurations.$HOST.config.system.build.kernel.outPath")"
stage=kernel_validate
"${nix_base[@]}" path-info --json "$kernel_path" >/dev/null
stage=initrd_eval
initrd_path="$("${nix_base[@]}" eval --raw --no-write-lock-file "$FLAKE#nixosConfigurations.$HOST.config.system.build.initialRamdisk.outPath")"
stage=initrd_validate
"${nix_base[@]}" path-info --json "$initrd_path" >/dev/null

stage=structured_output
printf 'NIXER_SOURCE_HEAD\t%s\n' "$source_head"
printf 'NIXER_SOURCE_DIRTY\t%s\n' "$dirty"
printf 'NIXER_SYSTEM_PATH\t%s\n' "$system_path"
printf 'NIXER_KERNEL_PATH\t%s\n' "$kernel_path"
printf 'NIXER_KERNEL_PATH_VALIDATED\ttrue\n'
printf 'NIXER_INITRD_PATH\t%s\n' "$initrd_path"
printf 'NIXER_INITRD_PATH_VALIDATED\ttrue\n'
printf 'NIXER_PATH_INFO_BEGIN\n'
stage=path_info
"${nix_base[@]}" path-info -S --json "$system_path"
stage=structured_output
printf '\nNIXER_PATH_INFO_END\n'

if [ -r "$system_path/boot.json" ]; then
    stage=bootspec_size
    bootspec_size="$(wc -c < "$system_path/boot.json")"
    stage=structured_output
    printf 'NIXER_BOOTSPEC_PATH\t%s\n' "$system_path/boot.json"
    printf 'NIXER_BOOTSPEC_SIZE\t%s\n' "$bootspec_size"
    if [ "$bootspec_size" -le 65536 ]; then
        printf 'NIXER_BOOTSPEC_BEGIN\n'
        stage=bootspec_read
        cat "$system_path/boot.json"
        stage=structured_output
        printf '\nNIXER_BOOTSPEC_END\n'
    else
        printf 'NIXER_BOOTSPEC_OMITTED\tover_size_limit\n'
    fi
else
    stage=structured_output
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


class _RedactedOutputSink:
    def __init__(
        self,
        mirror: TextIO,
        buffer: bytearray,
        limit: int,
        state: dict[str, bool],
        *,
        keep_tail: bool,
    ) -> None:
        self.mirror = mirror
        self.buffer = buffer
        self.limit = limit
        self.state = state
        self.keep_tail = keep_tail

    def write(self, text: str) -> int:
        raw = text.encode("utf-8", errors="replace")
        self.state["truncated"] |= _append_bounded(
            self.buffer, raw, self.limit, keep_tail=self.keep_tail
        )
        return self.mirror.write(text)

    def flush(self) -> None:
        self.mirror.flush()


class _RedactingLineMirror:
    def __init__(
        self,
        mirror: TextIO | _RedactedOutputSink,
        state: dict[str, bool] | None = None,
    ) -> None:
        self.mirror = mirror
        self.state = state
        self.pending = bytearray()
        self.pending_cr = False
        self.private_key_block = False
        self.secret_value_continuation = False
        self.secret_plain_value_base_indent: int | None = None
        self.secret_plain_value_started = False
        self.secret_quoted_value_quote: str | None = None
        self.secret_block_scalar_base_indent: int | None = None
        self.disabled = False

    @staticmethod
    def _private_key_marker(text: str, kind: str) -> bool:
        upper = text.upper()
        return f"-----{kind} " in upper and "PRIVATE KEY-----" in upper

    @staticmethod
    def _secret_label_without_value(text: str) -> bool:
        logical = text.rstrip("\r\n")
        return bool(
            re.search(
                r'''(?i)(?P<key_quote>["']?)(?:authorization|api[_-]?key|token|password|secret)(?P=key_quote)\s*[:=]\s*$''',
                logical,
            )
        )

    @staticmethod
    def _secret_plain_value_base_indent(text: str) -> int | None:
        logical = text.rstrip("\r\n")
        match = re.search(
            r"""(?i)^(?P<indent>[ \t]*)(?P<sequence>(?:-\s+)+)?(?P<key_quote>["']?)(?:authorization|api[_-]?key|token|password|secret)(?P=key_quote)\s*:\s*$""",
            logical,
        )
        if match is None:
            return None
        return len(match.group("indent")) + len(match.group("sequence") or "")

    @staticmethod
    def _secret_inline_plain_value_base_indent(text: str) -> int | None:
        logical = text.rstrip("\r\n")
        prefix = re.match(r"^(?P<indent>[ \t]*)(?P<sequence>(?:-\s+)+)?", logical)
        assert prefix is not None
        remainder = logical[prefix.end() :]
        match = re.search(
            r"""(?i)(?:^|[,{[]\s*)(?P<key_quote>["']?)(?:authorization|api[_-]?key|token|password|secret)(?P=key_quote)\s*:\s*(?P<value>[^"'|>\s].*)$""",
            remainder,
        )
        if match is None or match.group("value").lstrip().startswith("#"):
            return None
        return len(prefix.group("indent")) + len(prefix.group("sequence") or "")

    @staticmethod
    def _secret_block_scalar_base_indent(text: str) -> int | None:
        logical = text.rstrip("\r\n")
        match = re.search(
            r"""(?i)^(?P<indent>[ \t]*)(?P<sequence>(?:-\s+)+)?(?P<key_quote>["']?)(?:authorization|api[_-]?key|token|password|secret)(?P=key_quote)\s*[:=]\s*(?:(?:&[^\s]+|![^\s]+)\s+)*[|>](?:[+-][1-9]?|[1-9][+-]?)?\s*(?:#.*)?$""",
            logical,
        )
        if match is None:
            return None
        return len(match.group("indent")) + len(match.group("sequence") or "")

    @staticmethod
    def _find_unescaped_quote(text: str, quote: str) -> int | None:
        escaped = False
        index = 0
        while index < len(text):
            char = text[index]
            if escaped:
                escaped = False
                index += 1
                continue
            if char == "\\":
                escaped = True
                index += 1
                continue
            if char == quote:
                if quote == "'" and index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
                return index
            index += 1
        return None

    @classmethod
    def _secret_quoted_value(cls, text: str) -> tuple[str, str | None] | None:
        logical = text.rstrip("\r\n")
        match = re.search(
            r'''(?i)(?P<key_quote>["']?)(?:authorization|api[_-]?key|token|password|secret)(?P=key_quote)\s*[:=]\s*(?P<value_quote>["'])''',
            logical,
        )
        if match is None:
            return None
        quote = match.group("value_quote")
        closing = cls._find_unescaped_quote(logical[match.end() :], quote)
        if closing is None:
            return quote, None
        return quote, logical[match.end() + closing + 1 :]

    @staticmethod
    def _line_ending(text: str) -> str:
        if text.endswith("\r\n"):
            return "\r\n"
        if text.endswith("\n"):
            return "\n"
        if text.endswith("\r"):
            return "\r"
        return ""

    def _emit_line(self, raw: bytes) -> None:
        text = raw.decode("utf-8", errors="replace")
        ending = self._line_ending(text)
        begin = self._private_key_marker(text, "BEGIN")
        end = self._private_key_marker(text, "END")
        if self.private_key_block or begin:
            self.mirror.write("<REDACTED_PRIVATE_KEY_BLOCK>" + ending)
            self.private_key_block = (self.private_key_block or begin) and not end
            self.secret_value_continuation = False
            self.secret_plain_value_base_indent = None
            self.secret_quoted_value_quote = None
            self.secret_block_scalar_base_indent = None
        elif self.secret_block_scalar_base_indent is not None:
            logical = text[: -len(ending)] if ending else text
            if not logical.strip():
                self.mirror.write(ending)
            else:
                indent = len(logical) - len(logical.lstrip())
                if indent > self.secret_block_scalar_base_indent:
                    self.mirror.write("<REDACTED>" + ending)
                else:
                    self.secret_block_scalar_base_indent = None
                    self._emit_line(raw)
                    return
        elif self.secret_quoted_value_quote is not None:
            logical = text[: -len(ending)] if ending else text
            closing = self._find_unescaped_quote(logical, self.secret_quoted_value_quote)
            if closing is None:
                self.mirror.write("<REDACTED>" + ending)
            else:
                suffix = logical[closing + 1 :]
                self.mirror.write("<REDACTED>" + server._redact(suffix) + ending)
                self.secret_quoted_value_quote = None
        elif self.secret_value_continuation:
            logical = text[: -len(ending)] if ending else text
            stripped = logical.lstrip()
            if not stripped:
                self.mirror.write(ending)
            elif self.secret_plain_value_base_indent is not None:
                indent = len(logical) - len(logical.lstrip())
                if stripped.startswith("#"):
                    self.mirror.write("<REDACTED>" + ending)
                elif indent > self.secret_plain_value_base_indent:
                    self.secret_plain_value_started = True
                    self.mirror.write("<REDACTED>" + ending)
                elif self.secret_plain_value_started:
                    self.secret_value_continuation = False
                    self.secret_plain_value_base_indent = None
                    self.secret_plain_value_started = False
                    self._emit_line(raw)
                    return
                else:
                    if stripped[0] in {"\"", "'"}:
                        quote = stripped[0]
                        if self._find_unescaped_quote(stripped[1:], quote) is None:
                            self.secret_quoted_value_quote = quote
                    self.mirror.write("<REDACTED>" + ending)
                    self.secret_value_continuation = False
                    self.secret_plain_value_base_indent = None
                    self.secret_plain_value_started = False
            else:
                if stripped[0] in {"\"", "'"}:
                    quote = stripped[0]
                    if self._find_unescaped_quote(stripped[1:], quote) is None:
                        self.secret_quoted_value_quote = quote
                self.mirror.write("<REDACTED>" + ending)
                self.secret_value_continuation = False
                self.secret_plain_value_base_indent = None
        else:
            block_scalar_indent = self._secret_block_scalar_base_indent(text)
            quoted_value = self._secret_quoted_value(text)
            inline_plain_indent = self._secret_inline_plain_value_base_indent(text)
            if block_scalar_indent is not None:
                self.mirror.write("<REDACTED>" + ending)
                self.secret_block_scalar_base_indent = block_scalar_indent
                self.secret_quoted_value_quote = None
                self.secret_value_continuation = False
                self.secret_plain_value_base_indent = None
            elif quoted_value is not None:
                quote, suffix = quoted_value
                self.mirror.write(
                    "<REDACTED>"
                    + (server._redact(suffix) if suffix is not None else "")
                    + ending
                )
                self.secret_quoted_value_quote = quote if suffix is None else None
                self.secret_value_continuation = False
                self.secret_plain_value_base_indent = None
            elif inline_plain_indent is not None:
                self.mirror.write(server._redact(text))
                self.secret_value_continuation = True
                self.secret_plain_value_base_indent = inline_plain_indent
                self.secret_plain_value_started = True
            elif self._secret_label_without_value(text):
                self.mirror.write("<REDACTED>" + ending)
                self.secret_value_continuation = True
                self.secret_plain_value_base_indent = self._secret_plain_value_base_indent(text)
                self.secret_plain_value_started = False
            else:
                self.mirror.write(server._redact(text))
        self.mirror.flush()

    def feed(self, raw: bytes) -> None:
        if self.disabled:
            return
        for value in raw:
            if self.pending_cr:
                if value == 0x0A:
                    self._emit_line(bytes(self.pending) + b"\r\n")
                    self.pending.clear()
                    self.pending_cr = False
                    continue
                self._emit_line(bytes(self.pending) + b"\r")
                self.pending.clear()
                self.pending_cr = False
            if value == 0x0D:
                self.pending_cr = True
                continue
            if value == 0x0A:
                self._emit_line(bytes(self.pending) + b"\n")
                self.pending.clear()
                continue
            if len(self.pending) >= MAX_LIVE_LOG_LINE_BYTES:
                self.pending.clear()
                self.pending_cr = False
                self.disabled = True
                if self.state is not None:
                    self.state["truncated"] = True
                self.mirror.write("<REDACTED_OVERSIZED_LOG_STREAM>\n")
                self.mirror.flush()
                return
            self.pending.append(value)

    def finish(self) -> None:
        if self.disabled:
            return
        if self.pending_cr:
            self._emit_line(bytes(self.pending) + b"\r")
            self.pending.clear()
            self.pending_cr = False
        elif self.pending:
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
    redacting_mirror = None
    if mirror is not None:
        redacting_mirror = _RedactingLineMirror(
            _RedactedOutputSink(
                mirror,
                buffer,
                limit,
                state,
                keep_tail=keep_tail,
            ),
            state,
        )
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            raw = chunk.encode("utf-8", errors="replace") if isinstance(chunk, str) else bytes(chunk)
            if redacting_mirror is None:
                state["truncated"] |= _append_bounded(
                    buffer, raw, limit, keep_tail=keep_tail
                )
            else:
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
            "returncode": None,
            "stdout": "",
            "failure_class": "container_client_unavailable",
            "spawn_error": True,
            "spawn_errno": None,
            "stdout_truncated": False,
            "stderr_truncated": False,
        }
    except OSError as exc:
        return {
            "returncode": None,
            "stdout": "",
            "failure_class": "container_process_spawn_failed",
            "spawn_error": True,
            "spawn_errno": exc.errno,
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
    # Build stderr is operator-internal classification input only. Free-form
    # diagnostics never cross the evidence boundary or the live stderr channel.
    stderr_thread = threading.Thread(
        target=_pump,
        args=(process.stderr, stderr_buffer, MAX_FAILURE_DETAIL_BYTES, stderr_state),
        kwargs={"mirror": None, "keep_tail": True},
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    returncode = process.wait()
    stdout_thread.join()
    stderr_thread.join()

    stdout = stdout_buffer.decode("utf-8", errors="replace")
    stderr_internal = stderr_buffer.decode("utf-8", errors="replace")
    stage_failure = _parse_stage_failure(stderr_internal) if returncode == 88 else None
    failure_stage = stage_failure[0] if stage_failure is not None else None
    stage_exit_code = stage_failure[1] if stage_failure is not None else None
    if failure_stage == "nix_build" and stage_exit_code is not None:
        failure_class = _classify_failure(stderr_internal, stage_exit_code)
    elif returncode != 0 and stage_failure is None:
        failure_class = _classify_failure(stderr_internal, returncode)
    else:
        failure_class = None
    return {
        "returncode": returncode,
        "stdout": stdout,
        "failure_class": failure_class,
        "failure_stage": failure_stage,
        "stage_exit_code": stage_exit_code,
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
        "--cap-add=DAC_OVERRIDE",
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


def _require_structured_store_path(value: str | None, field: str) -> str:
    if value is None or _STRUCTURED_STORE_PATH_RE.fullmatch(value) is None:
        raise ValueError(f"{field} is not a validated Nix store path")
    return value


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
    verified_v1_paths: dict[str, str] | None = None,
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
    v1 = value.get("org.nixos.bootspec.v1")
    if isinstance(v1, dict):
        safe_v1: dict[str, str] = {}
        verified = verified_v1_paths or {}
        for key in _SAFE_BOOTSPEC_V1_PATH_FIELDS:
            expected = verified.get(key)
            item = v1.get(key)
            if expected is None or not isinstance(item, str) or item != expected:
                continue
            safe_v1[key] = item
        result["v1"] = safe_v1
    return result



def _parse_stage_failure(stderr: str) -> tuple[str, int] | None:
    matches = list(_EVIDENCE_STAGE_FAILURE_RE.finditer(stderr))
    for match in reversed(matches):
        stage = match.group(1)
        returncode = int(match.group(2))
        if stage in _EVIDENCE_FAILURE_STAGES and 1 <= returncode <= 255:
            return stage, returncode
    return None

def _classify_failure(stderr: str, returncode: int) -> str:
    lowered = stderr.lower()
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
    observed_at = server._utc_now()
    try:
        backend = server._backend_probe()
    except OSError as exc:
        failure: dict[str, Any] = {
            "class": "container_probe_spawn_failed",
            "detail": "container readiness probe could not be started",
        }
        if exc.errno is not None:
            failure["errno"] = exc.errno
        return {
            "schema_version": 1,
            "identity": EVIDENCE_IDENTITY,
            "operation": "system-build",
            "repo": str(root),
            "host": host_attr,
            "success": False,
            "status": "evidence_failed",
            "backend": _evidence_backend(),
            "failure": failure,
            "observed_at": observed_at,
        }
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
    if result.get("spawn_error"):
        failure = {
            "class": result["failure_class"],
            "detail": "container process could not be started",
        }
        if result.get("spawn_errno") is not None:
            failure["errno"] = result["spawn_errno"]
        return {
            "schema_version": 1,
            "identity": EVIDENCE_IDENTITY,
            "operation": "system-build",
            "repo": str(root),
            "host": host_attr,
            "success": False,
            "status": "evidence_failed",
            "backend": _evidence_backend(),
            "failure": failure,
            "observed_at": observed_at,
        }
    container_failures = {
        125: ("container_run_failed", "container client rejected the run request"),
        126: ("container_entrypoint_not_executable", "container entrypoint could not be invoked"),
        127: ("container_entrypoint_not_found", "container entrypoint could not be found"),
    }
    if result["returncode"] in container_failures:
        failure_class, detail = container_failures[result["returncode"]]
        return {
            "schema_version": 1,
            "identity": EVIDENCE_IDENTITY,
            "operation": "system-build",
            "repo": str(root),
            "host": host_attr,
            "success": False,
            "status": "evidence_failed",
            "backend": _evidence_backend(),
            "failure": {
                "class": failure_class,
                "container_exit_code": result["returncode"],
                "detail": detail,
                "detail_truncated": result["stderr_truncated"],
            },
            "observed_at": observed_at,
        }
    adapter_failures = {
        86: ("snapshot_head_mismatch", "source and snapshot Git HEAD differ"),
        87: ("invalid_system_output_path", "system build returned an invalid output path"),
    }
    if result["returncode"] in adapter_failures:
        failure_class, detail = adapter_failures[result["returncode"]]
        return {
            "schema_version": 1,
            "identity": EVIDENCE_IDENTITY,
            "operation": "system-build",
            "repo": str(root),
            "host": host_attr,
            "success": False,
            "status": "evidence_failed",
            "backend": _evidence_backend(),
            "failure": {
                "class": failure_class,
                "adapter_exit_code": result["returncode"],
                "detail": detail,
                "detail_truncated": result["stderr_truncated"],
            },
            "observed_at": observed_at,
        }
    failure_stage = result.get("failure_stage")
    stage_exit_code = result.get("stage_exit_code")
    if failure_stage == "nix_build" and stage_exit_code is not None:
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
                "class": result["failure_class"] or "nix_build_failed",
                "nix_exit_code": stage_exit_code,
                "detail": "build diagnostics suppressed by evidence boundary",
                "detail_truncated": result["stderr_truncated"],
            },
            "observed_at": observed_at,
        }
    if failure_stage is not None and stage_exit_code is not None:
        return {
            "schema_version": 1,
            "identity": EVIDENCE_IDENTITY,
            "operation": "system-build",
            "repo": str(root),
            "host": host_attr,
            "success": False,
            "status": "evidence_failed",
            "backend": _evidence_backend(),
            "failure": {
                "class": "adapter_stage_failed",
                "adapter_stage": failure_stage,
                "adapter_exit_code": stage_exit_code,
                "detail": "evidence adapter stage failed",
                "detail_truncated": result["stderr_truncated"],
            },
            "observed_at": observed_at,
        }
    if result["returncode"] != 0:
        return {
            "schema_version": 1,
            "identity": EVIDENCE_IDENTITY,
            "operation": "system-build",
            "repo": str(root),
            "host": host_attr,
            "success": False,
            "status": "evidence_failed",
            "backend": _evidence_backend(),
            "failure": {
                "class": "unclassified_container_exit",
                "container_exit_code": result["returncode"],
                "detail": "containerized evidence wrapper exited without a stage marker",
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
    kernel_path_validated = _field(stdout, "NIXER_KERNEL_PATH_VALIDATED")
    initrd_path = _field(stdout, "NIXER_INITRD_PATH")
    initrd_path_validated = _field(stdout, "NIXER_INITRD_PATH_VALIDATED")
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
        "kernel_path_validated": kernel_path_validated,
        "initrd_path": initrd_path,
        "initrd_path_validated": initrd_path_validated,
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
        if kernel_path_validated != "true" or initrd_path_validated != "true":
            raise ValueError("kernel/initrd store-path validation marker missing")
        kernel_path = _require_structured_store_path(kernel_path, "kernel_path")
        initrd_path = _require_structured_store_path(initrd_path, "initrd_path")
        system_path = _require_structured_store_path(system_path, "system_path")
        bootspec_size = int(bootspec_size_raw) if bootspec_size_raw is not None else None
        if bootspec_size is not None and bootspec_size < 0:
            raise ValueError("negative boot specification size")
        closure_size, path_info = _parse_path_info(path_info_raw)
        bootspec = _bootspec_summary(
            bootspec_raw,
            bootspec_path,
            verified_v1_paths={
                "kernel": kernel_path,
                "initrd": initrd_path,
                "toplevel": system_path,
            },
            size_bytes=bootspec_size,
            omitted_reason=bootspec_omitted,
        )
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError) as exc:
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
