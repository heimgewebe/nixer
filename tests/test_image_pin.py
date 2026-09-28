from __future__ import annotations

from pathlib import Path

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
                    else _probe(returncode=1)
                )
            if ref in {image_pin.PINNED_NIX_IMAGE_ID, image_pin.PINNED_NIX_IMAGE_REF} and image_present:
                return _probe(stdout=image_pin.PINNED_NIX_IMAGE_ID + "\n")
            return _probe(returncode=1)
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
            return _probe(returncode=1)
        if args[0] == "pull":
            pulled = True
            return _probe()
        if args[:3] == ["image", "inspect", "--format"] and args[-1] == image_pin.PINNED_NIX_IMAGE_ID:
            return _probe(stdout=wrong + "\n") if pulled else _probe(returncode=1)
        raise AssertionError(args)

    monkeypatch.setattr(image_pin, "_container_cli", lambda: Path("/usr/bin/docker"))
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(image_pin, "_run", fake_run)

    result = image_pin.ensure_pinned_image(pull_missing=True)

    assert result["ok"] is False
    assert result["status"] == "pinned_image_identity_mismatch"
    assert not any(call[0] == "tag" for call in calls)
