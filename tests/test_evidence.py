from __future__ import annotations

import io
import json
from pathlib import Path

import evidence


def test_system_build_script_reuses_snapshot_semantics() -> None:
    assert 'diff --binary --no-ext-diff --no-textconv HEAD -- > "$PATCH"' in evidence.SYSTEM_BUILD_SCRIPT
    assert '"$GIT" -C "$SNAPSHOT" add -A -- .' in evidence.SYSTEM_BUILD_SCRIPT
    assert "nixosConfigurations.$HOST.config.system.build.toplevel" in evidence.SYSTEM_BUILD_SCRIPT
    assert "--option build-users-group \"\"" in evidence.SYSTEM_BUILD_SCRIPT
    assert "--no-link --print-out-paths" in evidence.SYSTEM_BUILD_SCRIPT
    assert "path-info -S --json" in evidence.SYSTEM_BUILD_SCRIPT


def test_system_build_argv_is_fixed_and_hardened(tmp_path: Path) -> None:
    argv = evidence._system_build_argv(tmp_path, None, "heim-pc")
    assert argv[0] == str(evidence.server.DOCKER_BIN)
    assert "--pull=never" in argv
    assert "--cap-drop=ALL" in argv
    assert "--cap-add=SETUID" not in argv
    assert "--cap-add=SETGID" not in argv
    assert "--security-opt=no-new-privileges" in argv
    assert evidence.server.PINNED_NIX_IMAGE_ID in argv
    assert argv[-1] == "heim-pc"


def test_append_bounded_reports_actual_truncation_only() -> None:
    exact = bytearray()
    assert evidence._append_bounded(exact, b"abcd", 4, keep_tail=False) is False
    assert exact == b"abcd"

    clipped = bytearray()
    assert evidence._append_bounded(clipped, b"abcde", 4, keep_tail=False) is True
    assert clipped == b"abcd"

    tail = bytearray(b"ab")
    assert evidence._append_bounded(tail, b"cde", 4, keep_tail=True) is True
    assert tail == b"bcde"


def test_pump_redacts_mirrored_output() -> None:
    stream = io.BytesIO(b"token=secret-value-1234567890\n")
    mirror = io.StringIO()
    state = {"truncated": False}
    captured = bytearray()
    evidence._pump(stream, captured, 4096, state, mirror=mirror, keep_tail=True)
    assert "secret-value" not in mirror.getvalue()
    assert "<REDACTED>" in mirror.getvalue()


def test_parse_path_info_reads_closure_size() -> None:
    raw = json.dumps(
        [
            {
                "path": "/nix/store/example-nixos-system",
                "narSize": 100,
                "closureSize": 424242,
            }
        ]
    )
    closure_size, value = evidence._parse_path_info(raw)
    assert closure_size == 424242
    assert value[0]["path"] == "/nix/store/example-nixos-system"


def test_bootspec_summary_exposes_only_safe_v1_fields() -> None:
    raw = json.dumps(
        {
            "org.nixos.bootspec.v1": {
                "kernel": "/nix/store/kernel",
                "initrd": "/nix/store/initrd",
                "toplevel": "/nix/store/system",
                "kernelParams": ["example.secret=do-not-return"],
            },
            "foreign.extension": {"secret": "do-not-return"},
        }
    )
    summary = evidence._bootspec_summary(raw, "/nix/store/system/boot.json")
    assert summary["present"] is True
    assert summary["path"] == "/nix/store/system/boot.json"
    assert summary["top_level_keys"] == ["foreign.extension", "org.nixos.bootspec.v1"]
    assert summary["v1"]["kernel"] == "/nix/store/kernel"
    assert "kernelParams" not in summary["v1"]
    assert "foreign.extension" not in summary


def test_failure_classification() -> None:
    assert evidence._classify_failure("No space left on device", 1) == "disk_exhausted"
    assert evidence._classify_failure("error: builder for '/nix/store/x.drv' failed", 1) == "derivation_failed"
    assert (
        evidence._classify_failure(
            "flake does not provide attribute 'nixosConfigurations.missing'", 1
        )
        == "missing_nixos_configuration"
    )


def test_system_build_success_is_structured(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(evidence.server, "_resolve_repo", lambda _repo: tmp_path)
    monkeypatch.setattr(evidence.server, "_linked_git_common_dir", lambda _root: None)
    monkeypatch.setattr(
        evidence.server,
        "_backend_probe",
        lambda: {"ready": True, "image_id": evidence.server.PINNED_NIX_IMAGE_ID},
    )
    stdout = "\n".join(
        [
            "NIXER_SOURCE_HEAD\t" + "a" * 40,
            "NIXER_SOURCE_DIRTY\tfalse",
            "NIXER_SYSTEM_PATH\t/nix/store/system",
            "NIXER_KERNEL_PATH\t/nix/store/kernel",
            "NIXER_INITRD_PATH\t/nix/store/initrd",
            "NIXER_PATH_INFO_BEGIN",
            '[{"path":"/nix/store/system","closureSize":1234,"narSize":100}]',
            "NIXER_PATH_INFO_END",
            "NIXER_BOOTSPEC_PATH\t/nix/store/system/boot.json",
            "NIXER_BOOTSPEC_BEGIN",
            '{"org.nixos.bootspec.v1":{"kernel":"/nix/store/kernel"}}',
            "NIXER_BOOTSPEC_END",
            "",
        ]
    )
    monkeypatch.setattr(
        evidence,
        "_run_streaming",
        lambda _argv: {
            "returncode": 0,
            "stdout": stdout,
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
        },
    )

    result = evidence.system_build(str(tmp_path), "heim-pc")

    assert result["success"] is True
    assert result["source_observation"]["git_head"] == "a" * 40
    assert result["source_observation"]["authority"] == "observation_only"
    assert result["system"]["output_path"] == "/nix/store/system"
    assert result["system"]["closure_size_bytes"] == 1234
    assert result["boot"]["kernel"] == "/nix/store/kernel"
    assert result["boot"]["initrd"] == "/nix/store/initrd"
    assert result["boot"]["bootspec"]["present"] is True
    assert result["backend"]["build_users_group"] == "disabled"
    assert result["backend"]["lifecycle_owner"] == "external operator"


def test_system_build_failure_preserves_operator_lifecycle(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(evidence.server, "_resolve_repo", lambda _repo: tmp_path)
    monkeypatch.setattr(evidence.server, "_linked_git_common_dir", lambda _root: None)
    monkeypatch.setattr(evidence.server, "_backend_probe", lambda: {"ready": True})
    monkeypatch.setattr(
        evidence,
        "_run_streaming",
        lambda _argv: {
            "returncode": 1,
            "stdout": "",
            "stderr": "error: builder for '/nix/store/x.drv' failed",
            "stdout_truncated": False,
            "stderr_truncated": False,
        },
    )

    result = evidence.system_build(str(tmp_path), "heim-pc")

    assert result["success"] is False
    assert result["failure"]["class"] == "derivation_failed"
    assert result["backend"]["build_users_group"] == "disabled"
    assert result["backend"]["lifecycle_owner"] == "external operator"