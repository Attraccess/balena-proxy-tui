{
  description = "Fullscreen terminal app for temporary local access to balena devices";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs = { self, nixpkgs }:
    let
      systems = [ "aarch64-darwin" "x86_64-darwin" "aarch64-linux" "x86_64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      pkgsFor = system: import nixpkgs { inherit system; };
    in {
      packages = forAllSystems (system:
        let
          package = (pkgsFor system).callPackage ./nix/package.nix { };
        in {
          default = package;
          balena-proxy-tui = package;
        });

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/balena-proxy-tui";
          meta.description = "Open the balena proxy fullscreen terminal app";
        };
        balena = {
          type = "app";
          program = nixpkgs.lib.getExe (pkgsFor system).balena-cli;
          meta.description = "Run the bundled balena CLI, for example to log in";
        };
      });

      checks = forAllSystems (system: {
        package = self.packages.${system}.default;
      });

      overlays.default = final: _prev: {
        balena-proxy-tui = final.callPackage ./nix/package.nix { };
      };
    };
}
