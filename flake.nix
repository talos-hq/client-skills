{
  description = "Consent-gated sudo for Hermes agents (NixOS module + VM test)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      nixosModules.consent-sudo = ./nix/consent-sudo.nix;
      nixosModules.default = self.nixosModules.consent-sudo;

      overlays.default = final: prev: {
        hermes-consent-exec = final.callPackage ./nix/hermes-consent-exec.nix { };
      };

      packages = forAllSystems (pkgs: {
        hermes-consent-exec = pkgs.callPackage ./nix/hermes-consent-exec.nix { };
        default = self.packages.${pkgs.stdenv.hostPlatform.system}.hermes-consent-exec;
      });

      # `nix build .#checks.x86_64-linux.consent-sudo` boots a VM and asserts
      # the security properties from inside it. Needs KVM.
      checks = forAllSystems (pkgs: {
        consent-sudo = import ./tests/nixos/consent-sudo.nix { inherit pkgs; };
      });

      formatter = forAllSystems (pkgs: pkgs.nixfmt-rfc-style);
    };
}
