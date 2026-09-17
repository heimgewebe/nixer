from __future__ import annotations

from pathlib import Path

import server

ROOT = Path(__file__).resolve().parents[1]


def test_flake_exports_package_nixos_module_and_evidence_adapter() -> None:
    source = (ROOT / "flake.nix").read_text(encoding="utf-8")
    assert "packages = forAllSystems" in source
    assert "nixosModules.default" in source
    assert "checks = forAllSystems" in source
    assert 'version = "0.2.0"' in source
    assert 'version = "1.30.0"' in source
    assert "sha256-RFQUYl/OXClfqlBbsRus7OZhq29AKNV8k121eCC3o+Q=" in source
    assert 'pytestFlags = (old.pytestFlags or [ ]) ++ [ "-n" "0" ];' in source
    assert '"test_ws_client_exception_handling"' not in source
    assert "cp ${./evidence.py}" in source
    assert '"$out/bin/nixer-evidence"' in source


def test_nixos_module_keeps_mcp_loopback_only() -> None:
    source = (ROOT / "nix" / "module.nix").read_text(encoding="utf-8")
    assert "--host 127.0.0.1" in source
    assert "NIXER_DOCKER_BIN" in source
    assert "NIXER_CONTAINER_PATH" in source
    assert "NIXER_REPO_ROOT" in source
    assert "systemd.user.services.nixer" in source
    assert "ConditionUser" in source
    assert "NoNewPrivileges = true" not in source
    assert "ProtectSystem" not in source
    assert "PrivateTmp" not in source
    assert "RestrictAddressFamilies" not in source
    assert "RestrictSUIDSGID" not in source
    assert "LockPersonality" not in source


def test_snapshot_retracks_only_the_patch_materialized_in_the_ephemeral_clone() -> None:
    assert 'diff --binary --no-ext-diff --no-textconv HEAD -- > "$PATCH"' in server.SNAPSHOT_SCRIPT
    assert '"$GIT" -C "$SNAPSHOT" add -A -- .' in server.SNAPSHOT_SCRIPT


def test_packaging_does_not_add_activation_authority() -> None:
    combined = "\n".join(
        [
            (ROOT / "flake.nix").read_text(encoding="utf-8"),
            (ROOT / "nix" / "module.nix").read_text(encoding="utf-8"),
            (ROOT / "evidence.py").read_text(encoding="utf-8"),
        ]
    )
    assert "nixos-rebuild" not in combined
    assert "nixos-install" not in combined
    assert " switch " not in combined


def test_evidence_adapter_does_not_expand_mcp_surface() -> None:
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    assert '@mcp.tool(name="system_build"' not in source
    assert '@mcp.tool(name="system-build"' not in source
