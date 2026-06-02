{
  description = "rigx build for rigx";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.11";
  };

  outputs = { self, nixpkgs, ... }@inputs:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAll = f: nixpkgs.lib.genAttrs systems f;
      srcRoot = builtins.path {
        path = ./.;
        name = "source";
        filter = path: type:
          let base = baseNameOf (toString path); in
          !(builtins.elem base [
            ".rigx" "output" ".git" "result"
            "flake.nix" "flake.lock"
          ]);
      };
      installSrc = builtins.path {
        path = ./.;
        name = "rigx-install-src";
        filter = path: type:
          let base = baseNameOf (toString path); in
          !(builtins.elem base [
            ".rigx" "output" ".git" "result"
          ]);
      };
    in {
      packages = forAll (system:
        let
          pkgs = import nixpkgs { inherit system; };
          src = srcRoot;
        in rec {
          unittests = pkgs.stdenv.mkDerivation {
            pname = "unittests";
            version = "0.8.3";
            inherit src;
            buildInputs = [ pkgs.python3 ];
            dontConfigure = true;
            buildPhase = ''
              runHook preBuild
              python3 -m unittest discover tests
              runHook postBuild
            '';
            installPhase = ''
              mkdir -p $out
              touch $out/passed
            '';
          };
          rigx = pkgs.python3Packages.buildPythonApplication {
            pname = "rigx";
            version = "0.8.3";
            src = installSrc;
            pyproject = true;
            build-system = [ pkgs.python3Packages.setuptools ];
            doCheck = false;
            makeWrapperArgs = [ "--prefix" "PATH" ":" (pkgs.lib.makeBinPath [ pkgs.nix ]) ];
            meta.mainProgram = "rigx";
          };
          default = rigx;
        });
      apps = forAll (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/rigx";
        };
        rigx = self.apps.${system}.default;
      });
    };
}
