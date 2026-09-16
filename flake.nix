{
  description = "Nixer — narrow Nix/Nixpkgs/NixOS specialist";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs = { self, nixpkgs }:
    let
      supportedSystems = [ "x86_64-linux" ];
      forAllSystems = function:
        nixpkgs.lib.genAttrs supportedSystems (system:
          function (import nixpkgs { inherit system; }));

      packageFor = pkgs:
        let
          python = pkgs.python3.withPackages (ps: [ ps.mcp ]);
        in
        pkgs.stdenvNoCC.mkDerivation {
          pname = "nixer";
          version = "0.1.0";
          src = self;
          nativeBuildInputs = [ pkgs.makeWrapper ];
          dontBuild = true;

          installPhase = ''
            runHook preInstall
            mkdir -p "$out/bin" "$out/libexec/nixer"
            cp server.py "$out/libexec/nixer/server.py"
            makeWrapper ${python}/bin/python "$out/bin/nixer" \
              --add-flags "$out/libexec/nixer/server.py"
            runHook postInstall
          '';

          meta = {
            description = "Nix-only specialist MCP for Nix, Nixpkgs and NixOS";
            mainProgram = "nixer";
            platforms = nixpkgs.lib.platforms.linux;
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
        import ./nix/module.nix {
          inherit config lib pkgs;
          defaultPackage = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
        };
    };
}
