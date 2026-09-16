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
    assert "NIXER_BOOTSPEC_SIZE" in evidence.SYSTEM_BUILD_SCRIPT
    assert "over_size_limit" in evidence.SYSTEM_BUILD_SCRIPT


def test_system_build_argv_is_fixed_and_hardened(tmp_path: Path) -> None:
    argv = evidence._system_build_argv(tmp_path, None, "heim-pc")
    assert argv[0] == str(evidence.server.DOCKER_BIN)
    assert "--pull=never" in argv
    assert "--cap-drop=ALL" in argv
    assert "--cap-add=CHOWN" in argv
    assert "--cap-add=DAC_READ_SEARCH" in argv
    assert "--cap-add=FOWNER" in argv
    assert "--cap-add=DAC_OVERRIDE" in argv
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


def test_pump_redacts_quoted_multiword_credential_value() -> None:
    label = b"pass" + b"word"
    value = b'"correct ' + b"horse " + b"battery " + b'staple"'
    stream = io.BytesIO(label + b"=" + value + b"\n")
    mirror = io.StringIO()
    state = {"truncated": False}
    captured = bytearray()

    evidence._pump(stream, captured, 4096, state, mirror=mirror, keep_tail=True)

    detail = captured.decode("utf-8", errors="replace")
    live = mirror.getvalue()
    assert "correct horse battery staple" not in detail
    assert "correct horse battery staple" not in live
    assert "<REDACTED>" in detail
    assert "<REDACTED>" in live


def test_pump_redacts_quoted_credential_key() -> None:
    key = b'"' + b"pass" + b"word" + b'"'
    value = b'"correct ' + b"horse " + b"battery " + b'staple"'
    stream = io.BytesIO(b"{" + key + b":" + value + b"}\n")
    mirror = io.StringIO()
    state = {"truncated": False}
    captured = bytearray()

    evidence._pump(stream, captured, 4096, state, mirror=mirror, keep_tail=True)

    detail = captured.decode("utf-8", errors="replace")
    live = mirror.getvalue()
    assert "correct horse battery staple" not in detail
    assert "correct horse battery staple" not in live
    assert "<REDACTED>" in detail
    assert "<REDACTED>" in live


def test_pump_redacts_yaml_single_quoted_credential_with_doubled_quote() -> None:
    label = b"pass" + b"word"
    cases = (
        label + b": 'correct horse''s battery staple' suffix-visible\n",
        b"  - '" + label + b"': 'correct horse''s battery staple' suffix-visible\n",
    )

    for payload in cases:
        stream = io.BytesIO(payload + b"safe: visible\n")
        mirror = io.StringIO()
        state = {"truncated": False}
        captured = bytearray()

        evidence._pump(stream, captured, 4096, state, mirror=mirror, keep_tail=True)

        detail = captured.decode("utf-8", errors="replace")
        live = mirror.getvalue()
        for secret_fragment in ("correct horse", "battery staple"):
            assert secret_fragment not in detail
            assert secret_fragment not in live
        for visible in ("suffix-visible", "safe: visible"):
            assert visible in detail
            assert visible in live
        assert "<REDACTED>" in detail
        assert "<REDACTED>" in live


def test_pump_redacts_multiline_quoted_credential_value() -> None:
    label = b"pass" + b"word"
    stream = io.BytesIO(label + b'= "correct\nhorse battery staple" suffix\n')
    mirror = io.StringIO()
    state = {"truncated": False}
    captured = bytearray()

    evidence._pump(stream, captured, 4096, state, mirror=mirror, keep_tail=True)

    detail = captured.decode("utf-8", errors="replace")
    live = mirror.getvalue()
    assert "correct" not in detail
    assert "horse battery staple" not in detail
    assert "correct" not in live
    assert "horse battery staple" not in live
    assert "suffix" in detail
    assert "suffix" in live
    assert detail.count("<REDACTED>") >= 2
    assert live.count("<REDACTED>") >= 2


def test_pump_redacts_multiline_quoted_credential_value_across_bare_carriage_returns() -> None:
    label = b"pass" + b"word"
    stream = io.BytesIO(
        label + b'= "first-secret\rsecond-secret" suffix\rsafe: visible\r'
    )
    mirror = io.StringIO()
    state = {"truncated": False}
    captured = bytearray()

    evidence._pump(stream, captured, 4096, state, mirror=mirror, keep_tail=True)

    detail = captured.decode("utf-8", errors="replace")
    live = mirror.getvalue()
    for secret in ("first-secret", "second-secret"):
        assert secret not in detail
        assert secret not in live
    for visible in ("suffix", "safe: visible"):
        assert visible in detail
        assert visible in live
    assert detail.count("<REDACTED>") >= 2
    assert live.count("<REDACTED>") >= 2


def test_pump_redacts_yaml_block_scalar_credential() -> None:
    label = b"pass" + b"word"
    stream = io.BytesIO(
        label + b": |\n  first-secret-line\n  second-secret-line\nsafe: visible\n"
    )
    mirror = io.StringIO()
    state = {"truncated": False}
    captured = bytearray()

    evidence._pump(stream, captured, 4096, state, mirror=mirror, keep_tail=True)

    detail = captured.decode("utf-8", errors="replace")
    live = mirror.getvalue()
    for secret in ("first-secret-line", "second-secret-line"):
        assert secret not in detail
        assert secret not in live
    assert "safe: visible" in detail
    assert "safe: visible" in live
    assert detail.count("<REDACTED>") >= 3
    assert live.count("<REDACTED>") >= 3


def test_pump_redacts_yaml_block_scalar_with_modifiers() -> None:
    key = b'"' + b"pass" + b"word" + b'"'
    stream = io.BytesIO(
        b"  " + key + b": >2-\n    folded-secret-one\n    folded-secret-two\n  safe: visible\n"
    )
    mirror = io.StringIO()
    state = {"truncated": False}
    captured = bytearray()

    evidence._pump(stream, captured, 4096, state, mirror=mirror, keep_tail=True)

    detail = captured.decode("utf-8", errors="replace")
    live = mirror.getvalue()
    for secret in ("folded-secret-one", "folded-secret-two"):
        assert secret not in detail
        assert secret not in live
    assert "safe: visible" in detail
    assert "safe: visible" in live
    assert detail.count("<REDACTED>") >= 3
    assert live.count("<REDACTED>") >= 3


def test_pump_redacts_yaml_sequence_block_scalar_credential() -> None:
    label = b"pass" + b"word"
    cases = (
        b"  - " + label + b": |\n      list-secret-one\n      list-secret-two\n    safe: visible\n  - next: visible\n",
        b"  - \"" + label + b"\": >-\n      list-secret-one\n      list-secret-two\n    safe: visible\n  - next: visible\n",
        b"  - " + label + b": |2\n      list-secret-one\n      list-secret-two\n    safe: visible\n  - next: visible\n",
        b"  - '" + label + b"': >2-\n      list-secret-one\n      list-secret-two\n    safe: visible\n  - next: visible\n",
    )

    for payload in cases:
        stream = io.BytesIO(payload)
        mirror = io.StringIO()
        state = {"truncated": False}
        captured = bytearray()

        evidence._pump(stream, captured, 4096, state, mirror=mirror, keep_tail=True)

        detail = captured.decode("utf-8", errors="replace")
        live = mirror.getvalue()
        for secret in ("list-secret-one", "list-secret-two"):
            assert secret not in detail
            assert secret not in live
        for visible in ("safe: visible", "next: visible"):
            assert visible in detail
            assert visible in live
        assert detail.count("<REDACTED>") >= 3
        assert live.count("<REDACTED>") >= 3


def test_pump_redacts_nested_yaml_sequence_block_scalar_credential() -> None:
    label = b"pass" + b"word"
    cases = (
        b"- - " + label + b": |\n      nested-secret\n    safe: visible\n- next: visible\n",
        b"  - - - \"" + label + b"\": >2-\n          deeply-nested-secret\n        safe: visible\n  - next: visible\n",
    )

    for payload in cases:
        stream = io.BytesIO(payload)
        mirror = io.StringIO()
        state = {"truncated": False}
        captured = bytearray()

        evidence._pump(stream, captured, 4096, state, mirror=mirror, keep_tail=True)

        detail = captured.decode("utf-8", errors="replace")
        live = mirror.getvalue()
        for secret in ("nested-secret", "deeply-nested-secret"):
            assert secret not in detail
            assert secret not in live
        for visible in ("safe: visible", "next: visible"):
            assert visible in detail
            assert visible in live


def test_pump_redacts_quoted_label_value_continuation() -> None:
    key = b'"' + b"pass" + b"word" + b'"'
    stream = io.BytesIO(b"{" + key + b":\n" + b'"value-on-next-line"\n}')
    mirror = io.StringIO()
    state = {"truncated": False}
    captured = bytearray()

    evidence._pump(stream, captured, 4096, state, mirror=mirror, keep_tail=True)

    detail = captured.decode("utf-8", errors="replace")
    live = mirror.getvalue()
    assert "value-on-next-line" not in detail
    assert "value-on-next-line" not in live
    assert detail.count("<REDACTED>") >= 2
    assert live.count("<REDACTED>") >= 2


def test_pump_redacts_structured_tail_before_eviction() -> None:
    stream = io.BytesIO(b"x" * 64 + b" password=super-secret-value\n")
    mirror = io.StringIO()
    state = {"truncated": False}
    captured = bytearray()

    evidence._pump(stream, captured, 24, state, mirror=mirror, keep_tail=True)

    detail = captured.decode("utf-8", errors="replace")
    assert state["truncated"] is True
    assert "super-secret-value" not in detail
    assert "<REDACTED>" in detail
    assert "super-secret-value" not in mirror.getvalue()


def test_pump_redacts_secret_value_continuation_across_lines() -> None:
    stream = io.BytesIO(
        b"prefix\npassword=\nordinary-sensitive-value\ntrailer\n"
    )
    mirror = io.StringIO()
    state = {"truncated": False}
    captured = bytearray()

    evidence._pump(stream, captured, 128, state, mirror=mirror, keep_tail=True)

    detail = captured.decode("utf-8", errors="replace")
    live = mirror.getvalue()
    assert "ordinary-sensitive-value" not in detail
    assert "ordinary-sensitive-value" not in live
    assert "password=" not in detail
    assert "password=" not in live
    assert detail.count("<REDACTED>") >= 2
    assert live.count("<REDACTED>") >= 2
    assert "trailer" in detail
    assert "trailer" in live


def test_pump_preserves_secret_continuation_across_blank_line() -> None:
    stream = io.BytesIO(b"token:\n\nsecret-on-next-nonempty-line\n")
    mirror = io.StringIO()
    state = {"truncated": False}
    captured = bytearray()

    evidence._pump(stream, captured, 128, state, mirror=mirror, keep_tail=True)

    detail = captured.decode("utf-8", errors="replace")
    assert "secret-on-next-nonempty-line" not in detail
    assert "secret-on-next-nonempty-line" not in mirror.getvalue()


def test_redacting_line_mirror_redacts_across_chunk_boundary() -> None:
    mirror = io.StringIO()
    redactor = evidence._RedactingLineMirror(mirror)
    redactor.feed(b"credential=" + b"sk-" + b"proj-" + b"abcdefghijkl")
    redactor.feed(b"mnopqrstuvwxyz0123456789\n")
    redactor.finish()
    assert "sk-" not in mirror.getvalue()
    assert "<REDACTED>" in mirror.getvalue()


def test_redacting_line_mirror_preserves_split_crlf_boundaries() -> None:
    mirror = io.StringIO()
    redactor = evidence._RedactingLineMirror(mirror)

    redactor.feed(b'password="first-secret\r')
    assert mirror.getvalue() == ""
    redactor.feed(b'\nsecond-secret" suffix\r')
    redactor.feed(b'\nsafe: visible\r\n')
    redactor.finish()

    output = mirror.getvalue()
    assert "first-secret" not in output
    assert "second-secret" not in output
    assert "suffix" in output
    assert "safe: visible" in output
    assert output.count("\r\n") == 3


def test_redacting_line_mirror_suppresses_oversized_stream() -> None:
    mirror = io.StringIO()
    state = {"truncated": False}
    redactor = evidence._RedactingLineMirror(mirror, state)
    redactor.feed(b"x" * (evidence.MAX_LIVE_LOG_LINE_BYTES + 1))
    redactor.feed(b"\n" + b"to" + b"ken=value-that-must-not-appear\n")
    redactor.finish()
    assert mirror.getvalue() == "<REDACTED_OVERSIZED_LOG_STREAM>\n"
    assert state["truncated"] is True


def test_redacting_line_mirror_suppresses_private_key_block() -> None:
    mirror = io.StringIO()
    redactor = evidence._RedactingLineMirror(mirror)
    begin = b"-----BEGIN " + b"PRIVATE KEY-----\n"
    end = b"-----END " + b"PRIVATE KEY-----\n"
    redactor.feed(begin + b"key-material-that-must-not-appear\n")
    redactor.feed(end + b"after\n")
    redactor.finish()
    assert "key-material" not in mirror.getvalue()
    assert "after" in mirror.getvalue()


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
                "label": "password=bootspec-secret-value",
                "kernel": "/nix/store/kernel",
                "initrd": "/nix/store/initrd",
                "toplevel": "/nix/store/system",
                "kernelParams": ["example.secret=do-not-return"],
            },
            "foreign.extension": {"secret": "do-not-return"},
        }
    )
    summary = evidence._bootspec_summary(raw, "/nix/store/system/boot.json", size_bytes=len(raw))
    assert summary["present"] is True
    assert summary["path"] == "/nix/store/system/boot.json"
    assert "top_level_keys" not in summary
    assert summary["v1"]["kernel"] == "/nix/store/kernel"
    assert "bootspec-secret-value" not in summary["v1"]["label"]
    assert "<REDACTED>" in summary["v1"]["label"]
    assert "kernelParams" not in summary["v1"]
    assert "foreign.extension" not in summary


def test_bootspec_summary_omits_arbitrary_top_level_keys() -> None:
    secret_key = "to" + "ken=super-secret-top-level-value"
    raw = json.dumps(
        {
            "org.nixos.bootspec.v1": {"kernel": "/nix/store/kernel"},
            secret_key: {"extension": True},
        }
    )

    summary = evidence._bootspec_summary(raw, "/nix/store/system/boot.json", size_bytes=len(raw))

    assert "top_level_keys" not in summary
    assert summary["v1"]["kernel"] == "/nix/store/kernel"
    assert secret_key not in json.dumps(summary)


def test_bootspec_summary_reports_omitted_oversized_file() -> None:
    summary = evidence._bootspec_summary(
        None,
        "/nix/store/system/boot.json",
        size_bytes=evidence.MAX_BOOTSPEC_BYTES + 1,
        omitted_reason="over_size_limit",
    )
    assert summary["summary_status"] == "omitted"
    assert summary["omitted_reason"] == "over_size_limit"
    assert summary["size_bytes"] == evidence.MAX_BOOTSPEC_BYTES + 1


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


def test_system_build_returns_structured_failure_for_oversized_output(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(evidence.server, "_resolve_repo", lambda _repo: tmp_path)
    monkeypatch.setattr(evidence.server, "_linked_git_common_dir", lambda _root: None)
    monkeypatch.setattr(evidence.server, "_backend_probe", lambda: {"ready": True})
    monkeypatch.setattr(
        evidence,
        "_run_streaming",
        lambda _argv: {
            "returncode": 0,
            "stdout": "truncated",
            "stderr": "",
            "stdout_truncated": True,
            "stderr_truncated": False,
        },
    )
    result = evidence.system_build(str(tmp_path), "heim-pc")
    assert result["success"] is False
    assert result["status"] == "evidence_failed"
    assert result["failure"]["class"] == "structured_output_too_large"


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
