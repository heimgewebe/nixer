{ config, lib, pkgs, defaultPackage }:
let
  cfg = config.services.nixer;
in
{
  options.services.nixer = {
    enable = lib.mkEnableOption "the Nixer Nix/Nixpkgs/NixOS specialist MCP service";

    package = lib.mkOption {
      type = lib.types.package;
      default = defaultPackage;
      description = "Nixer package to execute.";
    };

    user = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "alex";
      description = ''
        User whose per-user systemd manager may start Nixer. This is required
        when the service is enabled so a global user-unit definition cannot
        race across multiple accounts for the same loopback port.
      '';
    };

    repoRoot = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "/home/alex/repos";
      description = ''
        Optional absolute repository root exported as NIXER_REPO_ROOT. When
        unset, Nixer derives <service-user-home>/repos at process start.
      '';
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 18187;
      description = "Loopback TCP port for Nixer's streamable HTTP MCP transport.";
    };

    containerCli = lib.mkOption {
      type = lib.types.str;
      default = "${pkgs.podman}/bin/podman";
      description = ''
        Absolute, service-manager-owned container CLI used by Nixer. This value
        is fixed at process start and is not exposed through the MCP surface.
        The selected runtime must already contain Nixer's pinned Nix image.
      '';
    };

    containerPath = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "/run/wrappers/bin" "${pkgs.podman}/bin" ];
      description = ''
        Explicit PATH passed to the container client. The NixOS wrapper path is
        required for rootless Podman's newuidmap/newgidmap cold-start helpers.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.user != null && cfg.user != "";
        message = "services.nixer.user must name the single account allowed to run Nixer";
      }
      {
        assertion = lib.hasPrefix "/" cfg.containerCli;
        message = "services.nixer.containerCli must be an absolute path";
      }
      {
        assertion = cfg.repoRoot == null || lib.hasPrefix "/" cfg.repoRoot;
        message = "services.nixer.repoRoot must be an absolute path when set";
      }
    ];

    systemd.user.services.nixer = {
      description = "Nixer Nix specialist MCP";
      wantedBy = [ "default.target" ];
      unitConfig = lib.optionalAttrs (cfg.user != null) {
        ConditionUser = cfg.user;
      };
      environment = {
        NIXER_DOCKER_BIN = cfg.containerCli;
        NIXER_CONTAINER_PATH = lib.concatStringsSep ":" cfg.containerPath;
      } // lib.optionalAttrs (cfg.repoRoot != null) {
        NIXER_REPO_ROOT = cfg.repoRoot;
      };
      serviceConfig = {
        Type = "simple";
        ExecStart = "${cfg.package}/bin/nixer --transport streamable-http --host 127.0.0.1 --port ${toString cfg.port}";
        Restart = "on-failure";
        RestartSec = 2;
        # This user service launches rootless Podman. Avoid parent-level mount/seccomp
        # hardening here because it can interfere with newuidmap/newgidmap on a cold
        # start. The launched Nix container remains capability-restricted and uses
        # no-new-privileges plus read-only repository mounts.
        UMask = "0077";
      };
    };
  };
}
