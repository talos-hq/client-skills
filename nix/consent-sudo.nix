# NixOS module: consent-gated sudo without touching /etc/sudoers.
#
# The imperative installer (scripts/install_consent_sudo.py) refuses on NixOS
# because sudoers there is generated. This module declares the same security
# properties instead:
#
#   * `security.sudo.extraRules` grants ONE command to ONE user, NOPASSWD,
#     with no argument wildcard;
#   * the helper is a pinned derivation on the system path (immutable store
#     path behind /run/current-system/sw/bin);
#   * the config, the approval state directory and the audit log are declared
#     as `/etc` content and tmpfiles, so a rebuild restores them;
#   * the helper still refuses unless a human approved the exact argv in
#     Telegram — the module only removes the need to write sudoers by hand.
#
# The agent must call the helper by the exact path named in the rule:
#   sudo -n /run/current-system/sw/bin/hermes-consent-exec run <id>
#
# Example:
#   services.hermesConsent = {
#     enable = true;
#     agentUser = "hermes";
#     telegramUserId = 123456789;   # the only Telegram user allowed to approve
#     ttlMinutes = 10;
#   };
{ config, lib, pkgs, ... }:

let
  cfg = config.services.hermesConsent;
  # /run/current-system/sw/bin is what the operator types and what sudo matches;
  # it is a root-owned symlink into the immutable store path of the package.
  helperPath = "/run/current-system/sw/bin/hermes-consent-exec";
in
{
  options.services.hermesConsent = {
    enable = lib.mkEnableOption "consent-gated sudo for a Hermes agent";

    agentUser = lib.mkOption {
      type = lib.types.str;
      example = "hermes";
      description = ''
        Unprivileged user that receives the single NOPASSWD rule. This is the
        user the agent runs as; it must not be root.
      '';
    };

    telegramUserId = lib.mkOption {
      type = lib.types.ints.positive;
      example = 123456789;
      description = ''
        Telegram user id allowed to approve escalations. Recorded in
        /etc/hermes-consent/config.json; the helper refuses any other approver.
      '';
    };

    ttlMinutes = lib.mkOption {
      type = lib.types.ints.between 0 1440;
      default = 10;
      description = "How long an approval stays valid. 0 expires immediately (testing only).";
    };

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./hermes-consent-exec.nix {
        src = ../scripts/hermes_consent_exec.py;
      };
      defaultText = lib.literalMD "`pkgs.callPackage ./hermes-consent-exec.nix { }`";
      description = "The root helper derivation.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.agentUser != "root";
        message = "services.hermesConsent.agentUser must not be root";
      }
    ];

    # On PATH as /run/current-system/sw/bin/hermes-consent-exec, which is the
    # exact string in the sudoers rule below.
    environment.systemPackages = [ cfg.package ];

    environment.etc."hermes-consent/config.json" = {
      mode = "0644";
      text = builtins.toJSON {
        agent_user = cfg.agentUser;
        telegram_user_id = cfg.telegramUserId;
        ttl_minutes = cfg.ttlMinutes;
      };
    };

    security.sudo.extraRules = [
      {
        users = [ cfg.agentUser ];
        commands = [
          {
            command = helperPath;
            options = [ "NOPASSWD" ];
          }
        ];
      }
    ];

    systemd.tmpfiles.rules = [
      "d /var/lib/hermes-consent 0700 root root -"
      "f /var/log/hermes-consent.log 0600 root root -"
    ];
  };
}
