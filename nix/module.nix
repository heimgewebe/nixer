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
  };

  config = lib.mkIf cfg.enable {
    systemd.user.services.nixer = {
      description = "Nixer Nix specialist MCP";
      wantedBy = [ "default.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      environment.NIXER_DOCKER_BIN = cfg.containerCli;
      serviceConfig = {
        Type = "simple";
        ExecStart = "${cfg.package}/bin/nixer --transport streamable-http --host 127.0.0.1 --port ${toString cfg.port}";
        Restart = "on-failure";
        RestartSec = 2;
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectSystem = "strict";
        ProtectHome = "read-only";
        RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" "AF_INET6" ];
        RestrictSUIDSGID = true;
        LockPersonality = true;
        UMask = "0077";
      };
    };
  };
}
