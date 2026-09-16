{
  description = "Nixer — narrow Nix/Nixpkgs/NixOS specialist";

  # Nixer owns its runtime dependency set. Host flakes pin Nixer as a product;
  # they do not substitute their own Nixpkgs into Nixer's Python environment.
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      supportedSystems = [ "x86_64-linux" ];
      forAllSystems = function:
        nixpkgs.lib.genAttrs supportedSystems (system:
          function (import nixpkgs { inherit system; }));

      packageFor = pkgs:
        let
          mcp130 = pkgs.python3Packages.mcp.overridePythonAttrs (_old: {
            version = "1.30.0";
            src = pkgs.fetchPypi {
              pname = "mcp";
              version = "1.30.0";
              hash = "sha256-RFQUYl/OXClfqlBbsRus7OZhq29AKNV8k121eCC3o+Q=";
            };
          });
          python = pkgs.python3.withPackages (_ps: [ mcp130 ]);
        in
        pkgs.stdenvNoCC.mkDerivation {
          pname = "nixer";
          version = "0.1.0";
          dontUnpack = true;
          nativeBuildInputs = [ pkgs.makeWrapper ];
          dontBuild = true;

          installPhase = ''
            runHook preInstall
            mkdir -p "$out/bin" "$out/libexec/nixer"
            cp ${./server.py} "$out/libexec/nixer/server.py"
            makeWrapper ${python}/bin/python "$out/bin/nixer" \
              --add-flags "$out/libexec/nixer/server.py"
            runHook postInstall
          '';

          meta = {
            description = "Nix-only specialist MCP for Nix, Nixpkgs and NixOS";
            mainProgram = "nixer";
            platforms = supportedSystems;
          };
        };
    in
    {
      packages = forAllSystems (pkgs: {
        default = packageFor pkgs;
        nixer = packageFor pkgs;
      });

      checks = forAllSystems (pkgs: {
        package = packageFor pkgs;
      });

      nixosModules.default = { config, lib, pkgs, ... }:
        let
          system = pkgs.stdenv.hostPlatform.system;
        in
        import ./nix/module.nix {
          inherit config lib pkgs;
          defaultPackage =
            if builtins.elem system supportedSystems then
              self.packages.${system}.default
            else
              throw "Nixer currently supports only: ${builtins.concatStringsSep ", " supportedSystems}";
        };
    };
}
