from __future__ import annotations

from pathlib import Path

import pytest

import image_pin
import server


def _probe(returncode: int = 0, stdout: str = "", stderr: str = "") -> dict[str, object]:
    return {"returncode": returncode, "stdout": stdout, "stderr": stderr}


def test_image_pin_contract_matches_mcp_runtime() -> None:
    assert image_pin.PINNED_NIX_IMAGE_ID == server.PINNED_NIX_IMAGE_ID
    assert image_pin.PINNED_NIX_IMAGE_TAG == server.PINNED_NIX_IMAGE_TAG
    assert image_pin.PINNED_NIX_IMAGE_REF == server.PINNED_NIX_IMAGE_REF


def test_existing_image_gets_local_retention_tag_without_pull(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(_cli: Path, args: list[str], *, timeout: int = 300):
        calls.append(args)
        if args[:3] == ["image", "inspect", "--format"] and args[-1] == image_pin.PINNED_NIX_IMAGE_TAG:
            tagged_before = any(call[0] == "tag" for call in calls[:-1])
            return _probe(
                stdout=(image_pin.PINNED_NIX_IMAGE_ID + "\n") if tagged_before else "",
                returncode=0 if tagged_before else 1,
                stderr="" if tagged_before else f"Error: No such image: {args[-1]}",
            )
        if args[:3] == ["image", "inspect", "--format"] and args[-1] in {
            image_pin.PINNED_NIX_IMAGE_ID,
            image_pin.PINNED_NIX_IMAGE_REF,
        }:
            return _probe(stdout=image_pin.PINNED_NIX_IMAGE_ID + "\n")
        if args[0] == "tag":
            return _probe()
        raise AssertionError(args)

    monkeypatch.setattr(image_pin, "_container_cli", lambda: Path("/usr/bin/docker"))
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(image_pin, "_run", fake_run)

    result = image_pin.ensure_pinned_image(pull_missing=True)

    assert result["ok"] is True
    assert result["pulled"] is False
    assert result["tagged"] is True
    assert ["tag", image_pin.PINNED_NIX_IMAGE_ID, image_pin.PINNED_NIX_IMAGE_TAG] in calls
    assert not any(call[0] == "pull" for call in calls)


def test_missing_image_pulls_only_immutable_ref_then_tags_verified_id(monkeypatch) -> None:
    calls: list[list[str]] = []
    image_present = False
    tag_present = False

    def fake_run(_cli: Path, args: list[str], *, timeout: int = 300):
        nonlocal image_present, tag_present
        calls.append(args)
        if args[0] == "pull":
            assert args == ["pull", image_pin.PINNED_NIX_IMAGE_REF]
            image_present = True
            return _probe()
        if args[0] == "tag":
            assert image_present
            tag_present = True
            return _probe()
        if args[:3] == ["image", "inspect", "--format"]:
            ref = args[-1]
            if ref == image_pin.PINNED_NIX_IMAGE_TAG:
                return (
                    _probe(stdout=image_pin.PINNED_NIX_IMAGE_ID + "\n")
                    if tag_present
                    else _probe(returncode=1, stderr=f"Error: No such image: {args[-1]}")
                )
            if ref in {image_pin.PINNED_NIX_IMAGE_ID, image_pin.PINNED_NIX_IMAGE_REF} and image_present:
                return _probe(stdout=image_pin.PINNED_NIX_IMAGE_ID + "\n")
            return _probe(returncode=1, stderr=f"Error: No such image: {args[-1]}")
        raise AssertionError(args)

    monkeypatch.setattr(image_pin, "_container_cli", lambda: Path("/usr/bin/docker"))
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(image_pin, "_run", fake_run)

    result = image_pin.ensure_pinned_image(pull_missing=True)

    assert result["ok"] is True
    assert result["pulled"] is True
    assert result["tagged"] is True
    assert ["pull", image_pin.PINNED_NIX_IMAGE_REF] in calls
    assert ["pull", image_pin.PINNED_NIX_IMAGE_TAG] not in calls


def test_conflicting_local_tag_fails_before_any_mutation(monkeypatch) -> None:
    wrong = "sha256:" + "1" * 64
    calls: list[list[str]] = []

    def fake_run(_cli: Path, args: list[str], *, timeout: int = 300):
        calls.append(args)
        if args[:3] == ["image", "inspect", "--format"] and args[-1] == image_pin.PINNED_NIX_IMAGE_TAG:
            return _probe(stdout=wrong + "\n")
        raise AssertionError(args)

    monkeypatch.setattr(image_pin, "_container_cli", lambda: Path("/usr/bin/docker"))
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(image_pin, "_run", fake_run)

    result = image_pin.ensure_pinned_image(pull_missing=True)

    assert result["ok"] is False
    assert result["status"] == "retention_tag_conflict"
    assert not any(call[0] in {"pull", "tag"} for call in calls)


def test_pull_with_wrong_resulting_identity_fails_without_tagging(monkeypatch) -> None:
    calls: list[list[str]] = []
    wrong = "sha256:" + "2" * 64
    pulled = False

    def fake_run(_cli: Path, args: list[str], *, timeout: int = 300):
        nonlocal pulled
        calls.append(args)
        if args[:3] == ["image", "inspect", "--format"] and args[-1] == image_pin.PINNED_NIX_IMAGE_TAG:
            return _probe(returncode=1, stderr=f"Error: No such image: {args[-1]}")
        if args[0] == "pull":
            pulled = True
            return _probe()
        if args[:3] == ["image", "inspect", "--format"] and args[-1] == image_pin.PINNED_NIX_IMAGE_ID:
            return _probe(stdout=wrong + "\n") if pulled else _probe(returncode=1, stderr=f"Error: No such image: {args[-1]}")
        raise AssertionError(args)

    monkeypatch.setattr(image_pin, "_container_cli", lambda: Path("/usr/bin/docker"))
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(image_pin, "_run", fake_run)

    result = image_pin.ensure_pinned_image(pull_missing=True)

    assert result["ok"] is False
    assert result["status"] == "pinned_image_identity_mismatch"
    assert not any(call[0] == "tag" for call in calls)


@pytest.mark.parametrize(
    ("scenario", "pull_missing", "expected_status", "may_tag"),
    [
        ("no_pull", False, "pinned_image_unavailable", False),
        ("pull_fails", True, "immutable_pull_failed", False),
        ("wrong_ref", True, "immutable_ref_identity_mismatch", False),
        ("tag_fails", True, "retention_tag_failed", True),
        ("tag_changed", True, "retention_tag_identity_mismatch", True),
        ("malformed_id", False, "pinned_image_unavailable", False),
        ("multiple_ids", False, "pinned_image_unavailable", False),
        ("tag_conflict", True, "retention_tag_conflict", False),
    ],
)
def test_fail_closed_image_provisioning_scenarios(
    monkeypatch, scenario, pull_missing, expected_status, may_tag
):
    """Adversarial responses may not produce success or unauthorized tagging."""
    calls: list[list[str]] = []
    tag_probes = 0
    image = image_pin.PINNED_NIX_IMAGE_ID
    tag = image_pin.PINNED_NIX_IMAGE_TAG
    ref = image_pin.PINNED_NIX_IMAGE_REF

    def fake_run(_cli: Path, args: list[str], *, timeout: int = 300):
        nonlocal tag_probes
        calls.append(args)
        if args[:3] == ["image", "inspect", "--format"]:
            target = args[-1]
            if target == tag:
                tag_probes += 1
                if scenario == "tag_conflict":
                    return _probe(stdout="sha256:" + "1" * 64)
                # A tag is absent during both the initial and pre-write
                # JIT probes; only show a new ID after an actual tag write.
                if not any(call[0] == "tag" for call in calls[:-1]):
                    return _probe(returncode=1, stderr=f"Error: No such image: {args[-1]}")
                if scenario == "tag_changed":
                    return _probe(stdout="sha256:" + "2" * 64)
                return _probe(stdout=image)
            if target == image:
                if scenario in ("no_pull", "pull_fails"):
                    return _probe(returncode=1, stderr=f"Error: No such image: {args[-1]}")
                if scenario == "malformed_id":
                    return _probe(stdout="noise " + image)
                if scenario == "multiple_ids":
                    return _probe(stdout=image + "\n" + image)
                return _probe(stdout=image)
            if target == ref:
                return _probe(stdout="sha256:" + "3" * 64 if scenario == "wrong_ref" else image)
        if args == ["pull", ref]:
            assert scenario == "pull_fails"
            return _probe(returncode=1, stderr="offline")
        if args == ["tag", image, tag]:
            assert scenario in ("tag_fails", "tag_changed")
            return _probe(returncode=1 if scenario == "tag_fails" else 0)
        raise AssertionError(args)

    monkeypatch.setattr(image_pin, "_container_cli", lambda: Path("/usr/bin/docker"))
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(image_pin, "_run", fake_run)

    result = image_pin.ensure_pinned_image(pull_missing=pull_missing)
    assert result["ok"] is False
    assert result["status"] == expected_status
    assert any(call[0] == "tag" for call in calls) is may_tag
    assert not any(call[0] == "pull" for call in calls if scenario != "pull_fails")


def test_invalid_container_client_path_fails_before_probe(monkeypatch):
    monkeypatch.setattr(image_pin, "_container_cli", lambda: Path("docker"))
    result = image_pin.ensure_pinned_image(pull_missing=True)
    assert (result["ok"], result["status"]) == (False, "container_client_path_not_absolute")
    monkeypatch.setattr(image_pin, "_container_cli", lambda: Path("/nonexistent/nixer-client"))
    result = image_pin.ensure_pinned_image(pull_missing=True)
    assert (result["ok"], result["status"]) == (False, "container_client_unavailable")


def test_real_subprocess_output_bound_invalid_utf8_timeout_and_oserror(monkeypatch):
    import subprocess
    import sys

    cli = Path(sys.executable)
    large = image_pin._run(cli, ["-c", "import os; os.write(1, b'x' * 200000)"], timeout=5)
    assert large["returncode"] == 0
    assert large["stdout"] == "x" * image_pin._MAX_DIAGNOSTIC_CHARS
    assert len(large["stderr"]) <= image_pin._MAX_DIAGNOSTIC_CHARS

    hostile = image_pin._run(
        cli, ["-c", "import os; os.write(1, bytes([255])); os.write(2, bytes([254]))"], timeout=5
    )
    assert hostile["returncode"] == 0
    assert "\ufffd" in hostile["stdout"] and "\ufffd" in hostile["stderr"]

    expired = image_pin._run(cli, ["-c", "import time; time.sleep(2)"], timeout=0.05)
    assert expired["returncode"] == 124
    assert "timeout" in expired["stderr"]

    def denied(*_args, **_kwargs):
        raise PermissionError("client path cannot be executed")

    monkeypatch.setattr(subprocess, "Popen", denied)
    refused = image_pin._run(cli, ["image", "inspect"], timeout=1)
    assert refused == {
        "returncode": 126, "stdout": "", "stderr": "container client execution failed"
    }


def test_oversized_inspect_stdout_is_fail_closed(monkeypatch):
    monkeypatch.setattr(
        image_pin, "_run",
        lambda *_args, **_kwargs: {
            "returncode": 125,
            "stdout": "",
            "stderr": "container image inspection output exceeded bound",
        },
    )
    image_id, output = image_pin._inspect_id(
        Path("/usr/bin/docker"), image_pin.PINNED_NIX_IMAGE_REF
    )
    assert image_id is None
    assert output["returncode"] == 125


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    [
        (0, "malformed", ""),
        (0, image_pin.PINNED_NIX_IMAGE_ID + "\n" + image_pin.PINNED_NIX_IMAGE_ID, ""),
        (124, "", "operation exceeded timeout"),
        (125, "", "container image inspection output exceeded bound"),
        (126, "", "container client execution failed"),
        (127, "", "container client unavailable"),
        (1, "", "temporary daemon error"),
        (125, "", "other podman error"),
        (125, "", "Error: permission denied; diagnostic says no such image"),
        (1, "", "Error: No such image: some-other/project:tag"),
        (125, "", "Error: nixos/nix:2.35.2: image not known; auth failed"),
        (125, "", "Permission denied\nError: No such image: nixos/nix:2.35.2"),
        (125, "", "Error: permission denied; no image found in logs"),
    ],
)
def test_ambiguous_retention_tag_probe_must_not_mutate(
    monkeypatch, returncode, stdout, stderr
):
    calls = []

    def fake_run(_cli: Path, args: list[str], *, timeout: int = 300):
        calls.append(args)
        assert args == [
            "image", "inspect", "--format", "{{.Id}}", image_pin.PINNED_NIX_IMAGE_TAG
        ]
        return _probe(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(image_pin, "_container_cli", lambda: Path("/usr/bin/docker"))
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(image_pin, "_run", fake_run)
    result = image_pin.ensure_pinned_image(pull_missing=True)
    assert (result["ok"], result["status"]) == (False, "retention_tag_probe_failed")
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("returncode", "stderr"),
    [
        (1, "Error: No such image: nixos/nix:2.35.2"),
        (125, "Error: nixos/nix:2.35.2: image not known"),
    ],
)
def test_missing_retention_tag_is_recognized_for_both_docker_and_podman(
    monkeypatch, returncode, stderr
):
    calls = []

    def fake_run(_cli: Path, args: list[str], *, timeout: int = 300):
        calls.append(args)
        if len(calls) == 1:
            assert args[-1] == image_pin.PINNED_NIX_IMAGE_TAG
            return _probe(returncode=returncode, stderr=stderr)
        assert args[-1] == image_pin.PINNED_NIX_IMAGE_ID
        return _probe(returncode=1, stderr=f"Error: No such image: {args[-1]}")

    monkeypatch.setattr(image_pin, "_container_cli", lambda: Path("/usr/bin/docker"))
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(image_pin, "_run", fake_run)
    result = image_pin.ensure_pinned_image(pull_missing=False)
    assert (result["ok"], result["status"]) == (False, "pinned_image_unavailable")
    assert len(calls) == 2

def test_real_image_inspect_oversize_and_invalid_utf8_rejected(tmp_path):
    import sys

    def stub(name: str, statement: str) -> Path:
        executable = tmp_path / name
        executable.write_text(
            f"#!{sys.executable}\nimport os\n{statement}\n", encoding="utf-8"
        )
        executable.chmod(0o700)
        return executable

    image = image_pin.PINNED_NIX_IMAGE_ID.encode("ascii")
    # Tail contains an apparently valid ID; the only safe result is rc125.
    noisy = stub("oversized-client", f"os.write(1, b'n' * 200000 + {image!r})")
    detected, probe = image_pin._inspect_id(noisy, image_pin.PINNED_NIX_IMAGE_TAG)
    assert detected is None
    assert probe["returncode"] == 125
    assert probe["stdout"] == ""

    invalid = stub("invalid-utf8-client", f"os.write(1, b'\\xff' + {image!r})")
    detected, probe = image_pin._inspect_id(invalid, image_pin.PINNED_NIX_IMAGE_TAG)
    assert detected is None
    assert probe["returncode"] == 0
    assert "\ufffd" in probe["stdout"]

    # A truncated stderr tail must never impersonate a complete missing-tag
    # diagnostic: the prefix may have contained the real daemon error.
    trailing = ("Error: No such image: " + image_pin.PINNED_NIX_IMAGE_TAG).encode("ascii")
    stderr_noisy = stub(
        "oversized-stderr-client",
        f"os.write(2, b'n' * 200000 + {trailing!r})",
    )
    detected, probe = image_pin._inspect_id(
        stderr_noisy, image_pin.PINNED_NIX_IMAGE_TAG
    )
    assert detected is None
    assert probe["returncode"] == 125
    assert not image_pin._proven_retention_tag_absent(probe)


def test_tag_created_while_pull_running_fails_before_overwrite(monkeypatch):
    """A slow immutable pull must not overwrite an intervening operator tag."""
    image = image_pin.PINNED_NIX_IMAGE_ID
    local_tag = image_pin.PINNED_NIX_IMAGE_TAG
    pinned_ref = image_pin.PINNED_NIX_IMAGE_REF
    conflicting = "sha256:" + "4" * 64
    calls = []
    state = {"pulled": False, "other_tag_created": False, "our_tag": False}

    def fake_run(_cli: Path, args: list[str], *, timeout: int = 300):
        calls.append(args)
        if args[:3] == ["image", "inspect", "--format"]:
            target = args[-1]
            if target == local_tag:
                if state["our_tag"]:
                    return _probe(stdout=image)
                if state["other_tag_created"]:
                    return _probe(stdout=conflicting)
                return _probe(returncode=1, stderr=f"Error: No such image: {args[-1]}")
            if target == image:
                return _probe(stdout=image) if state["pulled"] else _probe(
                    returncode=1, stderr=f"Error: No such image: {args[-1]}"
                )
            if target == pinned_ref:
                return _probe(stdout=image)
        if args == ["pull", pinned_ref]:
            state["pulled"] = True
            # Simulate an unrelated trusted operator tagging another local ID
            # during the 900s digest pull. This is not adapter authorization.
            state["other_tag_created"] = True
            return _probe()
        if args == ["tag", image, local_tag]:
            state["our_tag"] = True
            return _probe()
        raise AssertionError(args)

    monkeypatch.setattr(image_pin, "_container_cli", lambda: Path("/usr/bin/docker"))
    monkeypatch.setattr(Path, "is_file", lambda _self: True)
    monkeypatch.setattr(image_pin, "_run", fake_run)
    result = image_pin.ensure_pinned_image(pull_missing=True)
    assert result["ok"] is False
    assert result["status"] == "retention_tag_conflict"
    assert not state["our_tag"], "Nixer must not clobber a concurrent operator tag"
    assert not any(call[0] == "tag" for call in calls)


def test_ambiguous_jit_tag_probe_blocks_tag_mutation(monkeypatch):
    """The pre-tag recheck must reject misleading errors, not just the first probe."""
    image = image_pin.PINNED_NIX_IMAGE_ID
    tag = image_pin.PINNED_NIX_IMAGE_TAG
    ref = image_pin.PINNED_NIX_IMAGE_REF
    tag_probes = 0
    calls = []

    def fake_run(_cli: Path, args: list[str], *, timeout: int = 300):
        nonlocal tag_probes
        calls.append(args)
        if args[:3] == ["image", "inspect", "--format"]:
            if args[-1] == tag:
                tag_probes += 1
                if tag_probes == 1:
                    return _probe(returncode=1, stderr=f"Error: No such image: {tag}")
                return _probe(
                    returncode=125,
                    stderr=f"Error: permission denied; no such image mentioned for {tag}",
                )
            if args[-1] in {image, ref}:
                return _probe(stdout=image)
        raise AssertionError(f"Unexpected mutation or client call: {args}")

    monkeypatch.setattr(image_pin, "_container_cli", lambda: Path("/usr/bin/docker"))
    monkeypatch.setattr(Path, "is_file", lambda _self: True)
    monkeypatch.setattr(image_pin, "_run", fake_run)
    result = image_pin.ensure_pinned_image(pull_missing=False)
    assert (result["ok"], result["status"]) == (False, "retention_tag_probe_failed")
    assert tag_probes == 2
    assert not any(call[0] == "tag" for call in calls)
