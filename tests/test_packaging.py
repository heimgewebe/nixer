from __future__ import annotations

from pathlib import Path

import server

ROOT = Path(__file__).resolve().parents[1]


def test_flake_exports_package_and_nixos_module() -> None:
    source = (ROOT / "flake.nix").read_text(encoding="utf-8")
    assert "packages = forAllSystems" in source
    assert "nixosModules.default" in source
    assert "checks = forAllSystems" in source
    assert "ps.mcp" in source


def test_nixos_module_keeps_mcp_loopback_only() -> None:
    source = (ROOT / "nix" / "module.nix").read_text(encoding="utf-8")
    assert "--host 127.0.0.1" in source
    assert "NIXER_DOCKER_BIN" in source
    assert "systemd.user.services.nixer" in source
    assert "NoNewPrivileges = true" in source
    assert "ProtectHome = \"read-only\"" in source


def test_snapshot_retracks_only_the_patch_materialized_in_the_ephemeral_clone() -> None:
    assert 'diff --binary --no-ext-diff --no-textconv HEAD -- > "$PATCH"' in server.SNAPSHOT_SCRIPT
    assert '"$GIT" -C "$SNAPSHOT" add -A -- .' in server.SNAPSHOT_SCRIPT


def test_packaging_does_not_add_activation_authority() -> None:
    combined = "\n".join(
        [
            (ROOT / "flake.nix").read_text(encoding="utf-8"),
            (ROOT / "nix" / "module.nix").read_text(encoding="utf-8"),
        ]
    )
    assert "nixos-rebuild" not in combined
    assert "nixos-install" not in combined
    assert " switch " not in combined
