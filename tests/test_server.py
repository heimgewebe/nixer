# Nixer v0 authority and Nix-execution boundary tests.
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

import server


def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repos"
    repo = root / "fixture"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    monkeypatch.setattr(server, "REPO_ROOT", root.resolve())
    return repo


def _linked_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    root = tmp_path / "repos"
    common_repo = root / "source"
    common_dir = common_repo / ".git"
    git_dir = common_dir / "worktrees" / "fixture"
    git_dir.mkdir(parents=True)
    (git_dir / "commondir").write_text("../..\n", encoding="utf-8")

    repo = root / "fixture"
    repo.mkdir(parents=True)
    (repo / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    monkeypatch.setattr(server, "REPO_ROOT", root.resolve())
    return repo, common_dir.resolve()


def test_status_declares_nix_only_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_backend_probe", lambda: {"ready": True, "reason": "ready"})
    status = server.nixer_status()
    assert status["identity"] == "nixer-nix-specialist-v0"
    assert status["mode"] == "nix-only-specialist"
    assert status["scope"] == ["nix", "nixpkgs", "nixos"]
    assert "generic_shell" in status["forbidden_effects"]
    assert "repository_write" in status["forbidden_effects"]
    assert "real_nix_build" in status["forbidden_effects"]
    assert "nixos_switch_or_boot" in status["forbidden_effects"]
    assert "build_dry_run" in status["allowed_operations"]


def test_mcp_runner_allows_blocking_calls_to_overlap() -> None:
    lock = threading.Lock()
    both_started = threading.Event()
    release = threading.Event()
    active = 0

    def blocking(value: int) -> int:
        nonlocal active
        with lock:
            active += 1
            if active == 2:
                both_started.set()
        if not release.wait(timeout=2):
            raise AssertionError("concurrent MCP workers did not overlap")
        return value

    async def scenario() -> list[int]:
        tasks = [
            asyncio.create_task(server._run_mcp_tool(blocking, 1)),
            asyncio.create_task(server._run_mcp_tool(blocking, 2)),
        ]
        started = await asyncio.to_thread(both_started.wait, 2)
        if not started:
            release.set()
            raise AssertionError("blocking MCP calls remained serialized")
        release.set()
        return list(await asyncio.gather(*tasks))

    assert asyncio.run(scenario()) == [1, 2]


def test_mcp_runner_has_bounded_parallelism() -> None:
    assert server.MAX_CONCURRENT_MCP_TOOLS == 4
    assert isinstance(server._MCP_TOOL_SLOTS, threading.BoundedSemaphore)


def test_mcp_wrappers_preserve_public_tool_schema_names() -> None:
    wrappers = {
        server._mcp_nixer_status: "nixer_status",
        server._mcp_flake_metadata: "flake_metadata",
        server._mcp_flake_show: "flake_show",
        server._mcp_flake_check: "flake_check",
        server._mcp_eval_attr: "eval_attr",
        server._mcp_nixos_option: "nixos_option",
        server._mcp_derivation_show: "derivation_show",
        server._mcp_build_dry_run: "build_dry_run",
    }
    assert {wrapper.__name__ for wrapper in wrappers} == set(wrappers.values())


def test_repo_path_escape_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repos"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".git").mkdir()
    monkeypatch.setattr(server, "REPO_ROOT", root.resolve())
    with pytest.raises(PermissionError):
        server._resolve_repo(str(outside))


def test_repo_path_with_docker_mount_metacharacters_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repos"
    repo = root / "bad,repo"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    monkeypatch.setattr(server, "REPO_ROOT", root.resolve())
    with pytest.raises(PermissionError):
        server._resolve_repo(str(repo))


@pytest.mark.parametrize(
    "value",
    [
        "services.foo;builtins.abort",
        "services.foo/bar",
        "services.foo bar",
        "services.foo\nbar",
        "path:/tmp#evil",
        "",
    ],
)
def test_attr_injection_is_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        server._validate_attr_path(value, field="attribute")


def test_valid_nix_attr_paths_are_accepted() -> None:
    assert server._validate_attr_path("packages.x86_64-linux.default", field="attribute") == (
        "packages.x86_64-linux.default"
    )
    assert server._validate_attr_path("services.openssh.enable", field="option") == (
        "services.openssh.enable"
    )


def test_internal_runner_rejects_non_container_executables() -> None:
    with pytest.raises(ValueError):
        server._run(["/usr/bin/bash", "-lc", "true"])


def test_shared_redactor_handles_yaml_doubled_single_quotes() -> None:
    payload = "password: 'correct horse''s battery staple' suffix-visible"

    redacted = server._redact(payload)

    assert "correct horse" not in redacted
    assert "battery staple" not in redacted
    assert redacted == "<REDACTED> suffix-visible"


@pytest.mark.parametrize(
    "payload",
    [
        "password=abc,def",
        "token: abc;def",
    ],
)
def test_shared_redactor_consumes_punctuation_in_unquoted_credentials(payload: str) -> None:
    redacted = server._redact(payload)

    assert "abc" not in redacted
    assert "def" not in redacted
    assert redacted == "<REDACTED>"


def test_backend_probe_rejects_relative_container_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "DOCKER_BIN", Path("podman"))
    backend = server._backend_probe()
    assert backend["ready"] is False
    assert backend["reason"] == "container_client_path_not_absolute"


@pytest.mark.parametrize(
    "inspect_id",
    [
        server.PINNED_NIX_IMAGE_ID,
        server.PINNED_NIX_IMAGE_ID.removeprefix("sha256:"),
        server.PINNED_NIX_IMAGE_ID.upper().replace("SHA256:", "sha256:"),
    ],
)
def test_backend_probe_accepts_full_sha256_image_id_formats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inspect_id: str
) -> None:
    client = tmp_path / "podman"
    client.touch()
    monkeypatch.setattr(server, "DOCKER_BIN", client)
    monkeypatch.setattr(
        server,
        "_run",
        lambda *_args, **_kwargs: {
            "returncode": 0,
            "stdout": inspect_id + "\n",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
            "timed_out": False,
        },
    )
    backend = server._backend_probe()
    assert backend["ready"] is True
    assert backend["image_id"] == server.PINNED_NIX_IMAGE_ID


@pytest.mark.parametrize(
    "inspect_id",
    [
        "98edc6813218",
        "sha256:not-a-digest",
        "sha256:" + "0" * 64,
    ],
)
def test_backend_probe_rejects_invalid_or_different_image_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inspect_id: str
) -> None:
    client = tmp_path / "podman"
    client.touch()
    monkeypatch.setattr(server, "DOCKER_BIN", client)
    monkeypatch.setattr(
        server,
        "_run",
        lambda *_args, **_kwargs: {
            "returncode": 0,
            "stdout": inspect_id + "\n",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
            "timed_out": False,
        },
    )
    backend = server._backend_probe()
    assert backend["ready"] is False
    assert backend["reason"] == "pinned_image_identity_mismatch"
    assert backend["actual_image_id"] == inspect_id


def test_container_client_runtime_environment_is_startup_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = tmp_path / "podman"
    client.touch()
    captured: dict[str, object] = {}
    monkeypatch.setattr(server, "DOCKER_BIN", client)
    monkeypatch.setattr(server, "CONTAINER_PATH", "/run/wrappers/bin:/nix/store/podman/bin")
    monkeypatch.setattr(server, "CONTAINER_HOME", "/home/operator")
    monkeypatch.setattr(server, "CONTAINER_XDG_RUNTIME_DIR", "/run/user/1000")

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_subprocess_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return Completed()

    monkeypatch.setattr(server.subprocess, "run", fake_subprocess_run)
    result = server._run([str(client), "version"])
    assert result["returncode"] == 0
    assert captured["env"] == {
        "PATH": "/run/wrappers/bin:/nix/store/podman/bin",
        "HOME": "/home/operator",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
    }


def test_docker_nix_is_pinned_read_only_and_has_no_host_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    captured: list[str] = []

    monkeypatch.setattr(
        server,
        "_backend_probe",
        lambda: {"ready": True, "reason": "ready", "image_id": server.PINNED_NIX_IMAGE_ID},
    )

    def fake_run(argv: list[str], *, timeout: int = 120):
        captured.extend(argv)
        return {
            "returncode": 0,
            "stdout": "{}\n",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
            "timed_out": False,
        }

    monkeypatch.setattr(server, "_run", fake_run)
    result = server._docker_nix(
        str(repo),
        ["flake", "metadata", "--json", "--no-write-lock-file", server.FLAKE_SOURCE],
        operation="fixture",
        expect_json=True,
    )

    assert result["ok"] is True
    assert captured[0] == str(server.DOCKER_BIN)
    assert "--pull=never" in captured
    assert "--cap-drop=ALL" in captured
    assert "--cap-add=CHOWN" in captured
    assert "--cap-add=DAC_READ_SEARCH" in captured
    assert "--security-opt=no-new-privileges" in captured
    assert server.PINNED_NIX_IMAGE_ID in captured
    assert server.BASH_BIN in captured
    assert f"type=bind,src={repo},dst=/workspace,readonly" in captured
    assert "/var/run/docker.sock" not in " ".join(captured)
    assert server.FLAKE_SOURCE in captured
    assert result["backend"]["linked_worktree_git_metadata"] == "in-worktree"
    script = captured[captured.index("-lc") + 1]
    assert server.GIT_BIN in script
    assert server.NIX_BIN in script
    assert "clone --no-local --no-hardlinks" in script
    assert "diff --binary --no-ext-diff --no-textconv HEAD" in script
    assert 'allow-import-from-derivation false "$@"' in script


def test_linked_worktree_mounts_only_exact_common_git_dir_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, common_dir = _linked_repo(tmp_path, monkeypatch)
    captured: list[str] = []

    monkeypatch.setattr(
        server,
        "_backend_probe",
        lambda: {"ready": True, "reason": "ready", "image_id": server.PINNED_NIX_IMAGE_ID},
    )

    def fake_run(argv: list[str], *, timeout: int = 120):
        captured.extend(argv)
        return {
            "returncode": 0,
            "stdout": "{}\n",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
            "timed_out": False,
        }

    monkeypatch.setattr(server, "_run", fake_run)
    result = server._docker_nix(
        str(repo),
        ["flake", "metadata", "--json", "--no-write-lock-file", server.FLAKE_SOURCE],
        operation="fixture",
        expect_json=True,
    )

    mounts = [captured[index + 1] for index, value in enumerate(captured) if value == "--mount"]
    assert f"type=bind,src={repo},dst=/workspace,readonly" in mounts
    assert f"type=bind,src={common_dir},dst={common_dir},readonly" in mounts
    assert result["backend"]["linked_worktree_git_metadata"] == "read-only-common-dir"


def test_linked_worktree_git_dir_is_rejected_before_commondir_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repos"
    repo = root / "fixture"
    repo.mkdir(parents=True)
    outside_git_dir = tmp_path / "outside" / "worktrees" / "fixture"
    outside_git_dir.mkdir(parents=True)
    (repo / ".git").write_text(f"gitdir: {outside_git_dir}\n", encoding="utf-8")
    monkeypatch.setattr(server, "REPO_ROOT", root.resolve())

    with pytest.raises(PermissionError, match="Git directory is outside"):
        server._linked_git_common_dir(repo.resolve())


def test_linked_worktree_common_git_dir_must_stay_under_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repos"
    repo = root / "fixture"
    repo.mkdir(parents=True)
    outside_common = tmp_path / "outside" / ".git"
    git_dir = outside_common / "worktrees" / "fixture"
    git_dir.mkdir(parents=True)
    (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (repo / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    monkeypatch.setattr(server, "REPO_ROOT", root.resolve())

    with pytest.raises(PermissionError):
        server._linked_git_common_dir(repo.resolve())


def test_backend_unavailable_fails_closed_without_nix_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        server,
        "_backend_probe",
        lambda: {"ready": False, "reason": "pinned_image_unavailable"},
    )

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("Nix execution must not start when the pinned backend is unavailable")

    monkeypatch.setattr(server, "_run", must_not_run)
    result = server._docker_nix(str(repo), ["flake", "show"], operation="fixture")
    assert result["ok"] is False
    assert result["status"] == "backend_unavailable"
    assert result["backend"]["reason"] == "pinned_image_unavailable"


def test_flake_check_is_no_build(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_docker_nix(repo, args, *, operation, expect_json=False):
        captured.update(repo=repo, args=args, operation=operation, expect_json=expect_json)
        return {"ok": True}

    monkeypatch.setattr(server, "_docker_nix", fake_docker_nix)
    result = server.flake_check("heim-pc")
    assert result["ok"] is True
    assert "--no-build" in captured["args"]
    assert "--no-write-lock-file" in captured["args"]
    assert captured["args"][-1] == server.FLAKE_SOURCE
    assert captured["operation"] == "flake_check_no_build"


def test_build_tool_is_dry_run_only(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_docker_nix(repo, args, *, operation, expect_json=False):
        captured.update(repo=repo, args=args, operation=operation, expect_json=expect_json)
        return {"ok": True}

    monkeypatch.setattr(server, "_docker_nix", fake_docker_nix)
    server.build_dry_run("heim-pc", "packages.x86_64-linux.default")
    args = captured["args"]
    assert args[0] == "build"
    assert "--dry-run" in args
    assert "--no-link" in args
    assert captured["operation"] == "build_dry_run"


def test_nixos_option_constructs_only_validated_attribute(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_docker_nix(repo, args, *, operation, expect_json=False):
        captured.update(repo=repo, args=args, operation=operation, expect_json=expect_json)
        return {"ok": True}

    monkeypatch.setattr(server, "_docker_nix", fake_docker_nix)
    result = server.nixos_option("heim-pc", "heim-pc", "services.openssh.enable")
    assert result["attribute"] == "nixosConfigurations.heim-pc.config.services.openssh.enable"
    assert captured["args"][-1] == (
        f"{server.FLAKE_SOURCE}#nixosConfigurations.heim-pc.config.services.openssh.enable"
    )
    with pytest.raises(ValueError):
        server.nixos_option("heim-pc", "one.two", "services.openssh.enable")


def test_pinned_image_contract_matches_heim_pc_production_contract() -> None:
    assert server.PINNED_NIX_IMAGE_ID == (
        "sha256:98edc6813218e179ce84587373e0b52d4aa58babae2d26b51fb01e7fdacf815f"
    )
    assert server.PINNED_NIX_IMAGE_TAG == "nixos/nix:2.35.2"
    assert server.PINNED_NIX_IMAGE_REF == (
        "nixos/nix@sha256:7a007c766426c1877758ddc5cb87a965ac131fc78c582ce0083d922d51ae945c"
    )


def test_snapshot_contract_is_git_backed_and_excludes_untracked_source_files() -> None:
    assert server.FLAKE_SOURCE == "git+file:///tmp/nixer-workspace"
    assert "path:/workspace" not in server.FLAKE_SOURCE
    assert "clone --no-local --no-hardlinks" in server.SNAPSHOT_SCRIPT
    assert "diff --binary --no-ext-diff --no-textconv HEAD" in server.SNAPSHOT_SCRIPT
    assert 'exec "$NIX"' in server.SNAPSHOT_SCRIPT


def test_source_has_no_generic_shell_or_system_activation_primitive() -> None:
    source = Path(server.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "nixos-rebuild" not in source
    assert "nixos-install" not in source
    assert '"switch"' not in source
    assert '"boot"' not in source
