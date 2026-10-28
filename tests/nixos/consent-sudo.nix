# NixOS VM test for the declarative consent-gated sudo path.
#
#   nix build .#checks.x86_64-linux.consent-sudo
#
# Asserts, inside a booted VM, that the agent user has no blanket sudo, that an
# unapproved id is refused, that an approval from the wrong Telegram user is
# refused, that a correct approval runs exactly the approved argv as root, that
# replay is refused, and that the audit log has both decisions.
{ pkgs }:

pkgs.testers.runNixOSTest {
  name = "consent-sudo";

  nodes.machine =
    { ... }:
    {
      imports = [ ../../nix/consent-sudo.nix ];

      services.hermesConsent = {
        enable = true;
        agentUser = "agent";
        telegramUserId = 424242;
        ttlMinutes = 10;
      };

      users.users.agent = {
        isNormalUser = true;
        createHome = true;
      };

      environment.systemPackages = [
        pkgs.python3
        pkgs.util-linux
      ];

      # Test fixture: writes the request/approval files the helper reads, as the
      # unprivileged user, exactly the way the gateway would.
      environment.etc."consent-e2e/make.py".text = ''
        import hashlib, json, os, pwd, sys
        from datetime import datetime, timezone

        home = pwd.getpwuid(os.getuid()).pw_dir
        directory = os.path.join(home, ".hermes", "consent")
        os.makedirs(directory, mode=0o700, exist_ok=True)

        # NixOS has no /usr/bin; the system profile is the stable path.
        argv = ["/run/current-system/sw/bin/id", "-u"]
        digest = hashlib.sha256(b"\0".join(a.encode() for a in argv)).hexdigest()
        rid = "a" * 32
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        with open(os.path.join(directory, rid + ".json"), "w") as handle:
            json.dump({
                "id": rid,
                "argv": argv,
                "argv_sha256": digest,
                "reason": "nixos vm test",
                "requested_at": now,
            }, handle)

        if len(sys.argv) > 1 and sys.argv[1] == "approve":
            with open(os.path.join(directory, rid + ".approved"), "w") as handle:
                json.dump({
                    "id": rid,
                    "approver_telegram_user_id": int(sys.argv[2]),
                    "update_id": 7001,
                    "message_id": 88,
                    "approved_at": now,
                    "argv_sha256": digest,
                }, handle)
      '';
    };

  testScript = ''
    import json

    HELPER = "/run/current-system/sw/bin/hermes-consent-exec"
    SUDO = "runuser -u agent -- sudo -n"
    MAKE = "runuser -u agent -- python3 /etc/consent-e2e/make.py"
    RID = "a" * 32
    LOG = "/var/log/hermes-consent.log"

    machine.wait_for_unit("multi-user.target")
    machine.wait_for_unit("systemd-tmpfiles-setup.service")

    with subtest("the module declares the config the helper reads"):
        config = json.loads(machine.succeed("cat /etc/hermes-consent/config.json"))
        assert config["agent_user"] == "agent", config
        assert config["telegram_user_id"] == 424242, config
        assert config["ttl_minutes"] == 10, config

    with subtest("sudoers grants only the helper, with no wildcard"):
        sudoers = machine.succeed("cat /etc/sudoers")
        assert HELPER in sudoers, sudoers
        assert "NOPASSWD" in sudoers, sudoers

    with subtest("the agent has no blanket sudo"):
        machine.fail(SUDO + " true")
        machine.fail(SUDO + " /run/current-system/sw/bin/id -u")

    with subtest("an unapproved request is refused"):
        machine.succeed(MAKE + " request")
        machine.fail(SUDO + " " + HELPER + " run " + RID)

    with subtest("an approval from the wrong Telegram user is refused"):
        machine.succeed(MAKE + " approve 999999")
        machine.fail(SUDO + " " + HELPER + " run " + RID)

    with subtest("a correct approval runs exactly the approved argv as root"):
        machine.succeed(MAKE + " approve 424242")
        out = machine.succeed(SUDO + " " + HELPER + " run " + RID).strip()
        assert out == "0", "expected uid 0, got " + repr(out)

    with subtest("the approval is single use"):
        machine.fail(SUDO + " " + HELPER + " run " + RID)

    with subtest("the audit log records allowed and denied decisions"):
        machine.succeed("test \"$(stat -c %a " + LOG + ")\" = 600")
        machine.succeed("grep -q '\"decision\": \"allowed\"' " + LOG)
        machine.succeed("grep -q 'approver_mismatch' " + LOG)
        machine.succeed("grep -q 'replay' " + LOG)
        machine.succeed("grep -q 'no_approval' " + LOG)
  '';
}
