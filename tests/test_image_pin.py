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


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    [
        (124, "", "operation exceeded 30s timeout"),
        (126, "", "container client execution failed"),
        (127, "", "container client unavailable"),
        (125, "", "container image inspection output exceeded bound"),
        (1, "", "temporary daemon error"),
        (1, "", "Error: No such image: unrelated-image:other"),
        (1, "", "Error: permission denied; No such image: " + image_pin.PINNED_NIX_IMAGE_ID),
        (0, "invalid-image-id", ""),
        (0, image_pin.PINNED_NIX_IMAGE_ID + "\n" + image_pin.PINNED_NIX_IMAGE_ID, ""),
        (1, image_pin.PINNED_NIX_IMAGE_ID, "Error: No such image: " + image_pin.PINNED_NIX_IMAGE_ID),
        (124, "", "Error: No such image: " + image_pin.PINNED_NIX_IMAGE_ID),
    ],
)
def test_ambiguous_pinned_id_probe_must_not_pull(
    monkeypatch, returncode, stdout, stderr
):
    """An inconclusive inspect must never authorize even an immutable pull."""
    calls = []

    def fake_run(_cli: Path, args: list[str], *, timeout: int = 300):
        calls.append(args)
        if args[:3] == ["image", "inspect", "--format"]:
            if args[-1] == image_pin.PINNED_NIX_IMAGE_TAG:
                return _probe(
                    returncode=1,
                    stderr="Error: No such image: " + image_pin.PINNED_NIX_IMAGE_TAG,
                )
            if args[-1] == image_pin.PINNED_NIX_IMAGE_ID:
                return _probe(returncode=returncode, stdout=stdout, stderr=stderr)
        # No real container action is ever permitted by this mock.
        raise AssertionError(f"unexpected client effect: {args}")

    monkeypatch.setattr(image_pin, "_container_cli", lambda: Path("/usr/bin/docker"))
    monkeypatch.setattr(Path, "is_file", lambda _self: True)
    monkeypatch.setattr(image_pin, "_run", fake_run)
    result = image_pin.ensure_pinned_image(pull_missing=True)
    assert (result["ok"], result["status"]) == (False, "pinned_image_probe_failed")
    assert len(calls) == 2
    assert all(call[0] == "image" for call in calls)


@pytest.mark.parametrize(
    ("returncode", "stderr"),
    [
        (1, "Error response from daemon: No such image: " + image_pin.PINNED_NIX_IMAGE_ID),
        (125, "Error: " + image_pin.PINNED_NIX_IMAGE_ID + ": image not known"),
    ],
)
def test_proven_pinned_id_absence_may_pull_exact_digest(
    monkeypatch, returncode, stderr
):
    """Only the exact missing-ID diagnostic permits a digest-pinned pull."""
    calls = []
    pulled = False
    tagged = False

    def fake_run(_cli: Path, args: list[str], *, timeout: int = 300):
        nonlocal pulled, tagged
        calls.append(args)
        if args[:3] == ["image", "inspect", "--format"]:
            if args[-1] == image_pin.PINNED_NIX_IMAGE_TAG:
                return (
                    _probe(stdout=image_pin.PINNED_NIX_IMAGE_ID)
                    if tagged else _probe(
                        returncode=1,
                        stderr="Error: No such image: " + image_pin.PINNED_NIX_IMAGE_TAG,
                    )
                )
            if args[-1] == image_pin.PINNED_NIX_IMAGE_ID:
                return (
                    _probe(stdout=image_pin.PINNED_NIX_IMAGE_ID)
                    if pulled else _probe(returncode=returncode, stderr=stderr)
                )
            if args[-1] == image_pin.PINNED_NIX_IMAGE_REF:
                return _probe(stdout=image_pin.PINNED_NIX_IMAGE_ID)
        if args == ["pull", image_pin.PINNED_NIX_IMAGE_REF]:
            pulled = True
            return _probe()
        if args == ["tag", image_pin.PINNED_NIX_IMAGE_ID, image_pin.PINNED_NIX_IMAGE_TAG]:
            tagged = True
            return _probe()
        raise AssertionError(args)

    monkeypatch.setattr(image_pin, "_container_cli", lambda: Path("/usr/bin/docker"))
    monkeypatch.setattr(Path, "is_file", lambda _self: True)
    monkeypatch.setattr(image_pin, "_run", fake_run)
    result = image_pin.ensure_pinned_image(pull_missing=True)
    assert (result["ok"], result["pulled"], result["tagged"]) == (True, True, True)
    assert calls.count(["pull", image_pin.PINNED_NIX_IMAGE_REF]) == 1
    assert not any(call == ["pull", image_pin.PINNED_NIX_IMAGE_TAG] for call in calls)


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


def test_timed_out_client_reaps_its_inherited_pipe_descendants(tmp_path):
    """Timeout must not leave a grandchild running with inherited client pipes."""
    import os
    import signal
    import sys
    import time

    if not Path("/proc").is_dir():
        pytest.skip("Linux /proc required for descendant liveness check")

    pidfile = tmp_path / "client-child.pid"
    child = "import time; time.sleep(10)"
    parent = (
        "import pathlib, subprocess, sys; "
        f"child = subprocess.Popen([sys.executable, '-c', {child!r}], "
        "stdout=sys.stdout, stderr=sys.stderr); "
        f"pathlib.Path({str(pidfile)!r}).write_text(str(child.pid))"
    )

    def running(pid: int) -> bool:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().split()[2]
        except FileNotFoundError:
            return False
        return state != "Z"

    try:
        response = image_pin._run(Path(sys.executable), ["-c", parent], timeout=5.0)
        assert response["returncode"] == 124
        assert pidfile.exists()
        pid = int(pidfile.read_text())
        for _ in range(30):
            if not running(pid):
                break
            time.sleep(0.02)
        assert not running(pid), "timed-out client leaked an active descendant"
    finally:
        if pidfile.exists():
            pid = int(pidfile.read_text())
            if running(pid):
                os.kill(pid, signal.SIGKILL)


def test_real_missing_docker_inspect_newline_allows_only_exact_absence(tmp_path):
    """Docker's missing-image template emits a newline even when inspect exits 1."""
    import sys

    stub = tmp_path / "missing-docker-client"
    script = (
        f"#!{sys.executable}\n"
        "import sys\n"
        "sys.stdout.write('\\n')\n"
        "sys.stderr.write('Error response from daemon: No such image: "
        + image_pin.PINNED_NIX_IMAGE_TAG + "')\n"
        "raise SystemExit(1)\n"
    )
    stub.write_text(script, encoding="utf-8")
    stub.chmod(0o700)

    image, probe = image_pin._inspect_id(stub, image_pin.PINNED_NIX_IMAGE_TAG)
    assert image is None
    assert (probe["returncode"], probe["stdout"]) == (1, "\n")
    assert image_pin._proven_retention_tag_absent(probe)
    # Whitespace-only is compatible with Docker, not arbitrary stdout noise.
    assert not image_pin._proven_retention_tag_absent({
        **probe, "stdout": "noise\n",
    })


@pytest.mark.parametrize("client_exit_code", [124, 126])
def test_legitimate_cli_exit_codes_do_not_kill_process_groups(
    monkeypatch, client_exit_code
):
    """An ordinary client exit 124/126 is not a timeout or pipe failure."""
    import sys

    kills = []

    def fake_killpg(pid, sig):
        kills.append((pid, sig))

    monkeypatch.setattr(image_pin.os, "killpg", fake_killpg)
    result = image_pin._run(
        Path(sys.executable),
        ["-c", f"import sys; sys.exit({client_exit_code})"],
        timeout=5,
    )
    assert result["returncode"] == client_exit_code
    assert result["stderr"] == ""
    assert kills == [], "ordinary CLI exit must never kill a process group"


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_interrupt_during_post_spawn_deadline_reaps_client_group(
    monkeypatch, exception_type
):
    """An interrupt at the first post-Popen monotonic call must not orphan a group."""
    import signal

    class FakePipe:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FakeProcess:
        pid = 434343

        def __init__(self):
            self.terminated = False
            self.direct_kills = 0
            self.waits = 0
            self.stdout = FakePipe()
            self.stderr = FakePipe()

        def poll(self):
            return -signal.SIGKILL if self.terminated else None

        def kill(self):
            self.direct_kills += 1
            self.terminated = True

        def wait(self, timeout=None):
            self.waits += 1
            return -signal.SIGKILL

    proc = FakeProcess()
    groups = []
    clock_calls = 0
    real_monotonic = image_pin.time.monotonic

    def first_clock_call_interrupts():
        nonlocal clock_calls
        clock_calls += 1
        if clock_calls == 1:
            raise exception_type()
        return real_monotonic()

    def spawn(*_args, **kwargs):
        assert kwargs.get("start_new_session") is True
        return proc

    def kill_group(pid, sig):
        groups.append((pid, sig))
        proc.terminated = True

    monkeypatch.setattr(image_pin.subprocess, "Popen", spawn)
    monkeypatch.setattr(image_pin.time, "monotonic", first_clock_call_interrupts)
    monkeypatch.setattr(image_pin.os, "killpg", kill_group)
    with pytest.raises(exception_type):
        image_pin._run(Path("/usr/bin/docker"), ["image", "inspect", "test"], timeout=5)

    assert clock_calls >= 1
    assert groups == [(proc.pid, signal.SIGKILL)]
    assert proc.direct_kills == 0
    assert proc.waits == 1
    assert proc.stdout.closed and proc.stderr.closed


def test_keyboard_interrupt_reaps_isolated_container_client_group(monkeypatch):
    """Ctrl-C must terminate the whole new-session group, not only Popen's child."""
    import signal

    class FakePipe:
        closed = False

        def close(self):
            self.closed = True

    class FakeProcess:
        pid = 424242

        def __init__(self):
            self.terminated = False
            self.direct_kills = 0
            self.waits = 0
            self.stdout = FakePipe()
            self.stderr = FakePipe()

        def poll(self):
            return -signal.SIGKILL if self.terminated else None

        def kill(self):
            self.direct_kills += 1
            self.terminated = True

        def wait(self, timeout=None):
            self.waits += 1
            return -signal.SIGKILL

    class InterruptingSelector:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def register(self, *_args):
            return None

        def get_map(self):
            return {"stdout": 1, "stderr": 2}

        def select(self, *_args):
            raise KeyboardInterrupt()

    proc = FakeProcess()
    groups = []

    def launch(*_args, **kwargs):
        assert kwargs.get("start_new_session") is True
        return proc

    def signal_group(pid, sig):
        groups.append((pid, sig))
        proc.terminated = True

    monkeypatch.setattr(image_pin.subprocess, "Popen", launch)
    monkeypatch.setattr(image_pin.selectors, "DefaultSelector", InterruptingSelector)
    monkeypatch.setattr(image_pin.os, "killpg", signal_group)

    with pytest.raises(KeyboardInterrupt):
        image_pin._run(Path("/usr/bin/docker"), ["image", "inspect", "test"], timeout=5)

    assert groups == [(proc.pid, signal.SIGKILL)]
    assert proc.direct_kills == 0
    assert proc.waits == 1
    assert proc.stdout.closed and proc.stderr.closed

@pytest.mark.parametrize(
    "stop_signals,ignore_hup",
    [((15,), False), ((1,), False), ((15, 1), False), ((2, 15), False),
     ((1, 15), True)],
    ids=["sigterm", "sighup", "sigterm-then-sighup", "sigint-then-sigterm",
         "inherited-sighup-ignored"],
)
def test_cli_termination_reaps_isolated_client_and_descendant(
    tmp_path, stop_signals, ignore_hup
):
    """Real termination of the CLI must not orphan the isolated client group."""
    import os
    import signal
    import subprocess
    import sys
    import time

    if not Path("/proc").is_dir():
        pytest.skip("Linux /proc required for descendant liveness check")

    pidfile = tmp_path / "client-group.pids"
    client = tmp_path / "fake-container-client"
    client.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, signal, subprocess, sys, time\n"
        "assert signal.SIGTERM not in signal.pthread_sigmask(signal.SIG_BLOCK, set())\n"
        "assert signal.SIGHUP not in signal.pthread_sigmask(signal.SIG_BLOCK, set())\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
        "stdout=sys.stdout, stderr=sys.stderr)\n"
        f"pathlib.Path({str(pidfile)!r}).write_text(f'{{os.getpid()}} {{child.pid}}')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    client.chmod(0o700)

    def running(pid):
        try:
            return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
        except FileNotFoundError:
            return False

    command = [sys.executable, str(Path(image_pin.__file__))]
    if ignore_hup:
        # Set SIG_IGN in the parent process, then exec the actual CLI.
        # exec preserves ignored dispositions, unlike Python signal handlers.
        prelude = (
            "import os, signal, sys; "
            "signal.signal(signal.SIGHUP, signal.SIG_IGN); "
            "os.execv(sys.executable, [sys.executable, sys.argv[1]])"
        )
        command = [sys.executable, "-c", prelude, str(Path(image_pin.__file__))]
    process = subprocess.Popen(
        command,
        env={**os.environ, "NIXER_DOCKER_BIN": str(client)},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    pids = []
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if pidfile.exists():
                try:
                    pids = [int(pid) for pid in pidfile.read_text().split()]
                except ValueError:
                    pids = []
                if len(pids) == 2:
                    break
            time.sleep(0.02)
        assert len(pids) == 2, "fake container client pidfile incomplete"
        assert all(running(pid) for pid in pids)
        if ignore_hup:
            process.send_signal(signal.SIGHUP)
            time.sleep(0.25)
            assert process.poll() is None, "inherited SIG_IGN unexpectedly terminated CLI"
            assert all(running(pid) for pid in pids)
            process.send_signal(signal.SIGTERM)
        else:
            for stop_signal in stop_signals:
                process.send_signal(stop_signal)
        process.wait(timeout=5)
        deadline = time.monotonic() + 2
        while any(running(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.02)
        if ignore_hup:
            assert process.returncode == 128 + signal.SIGTERM
        else:
            assert process.returncode in {128 + sig for sig in stop_signals}
        assert not any(running(pid) for pid in pids), "termination orphaned client group"
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for pid in pids:
            if running(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()