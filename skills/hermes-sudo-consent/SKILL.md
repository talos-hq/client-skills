---
name: hermes-sudo-consent
description: "Use when a fix needs root. Get a human tap, then escalate."
version: 0.1.0
author: Talos HQ
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [client, sudo, consent, escalation, audit]
    category: client
    related_skills: []
---

# Consent-gated sudo

Runs one root command, once, only after the human taps approve in Telegram — and leaves a JSON audit line for it.

## When to use

- The fix genuinely needs root and there is no user-level route: `modprobe`/DKMS rebuild after a kernel upgrade, `systemctl restart` of a *system* unit, repairing a package, writing under `/etc`, a driver that failed to load (the wedged-GPU case).
- You already have the symptom: `must be root`, `EPERM` on `/sys` or `/etc`, `Operation not permitted` from a tool that documents root.
- The human is reachable in Telegram and can tap a button.

**Do not use when** the task runs fine as your own user (restart a `--user` unit, edit files in `$HOME`), or when nobody can approve — then hand the human a copy-pasteable command and stop.

## Prerequisites

| Need | How to check |
|---|---|
| Helper installed (or the NixOS module enabled) | `sudo -n /usr/local/libexec/hermes-consent-exec run 0000…` answers `bad_id`/`no_request`, not `password is required` |
| Agent CLI | `uv run --no-project scripts/hermes_consent_agent.py list` |
| The human's Telegram id is the configured approver | `cat /etc/hermes-consent/config.json` |
| NixOS only | helper is `/run/current-system/sw/bin/hermes-consent-exec`; export `HERMES_CONSENT_HELPER` to that path |

## Procedure

1. **Request the exact argv.** Absolute `argv[0]`, smallest command that fixes it:

   ```bash
   uv run --no-project scripts/hermes_consent_agent.py request \
     --reason "nvidia module missing after kernel upgrade; rebuild + load" -- \
     /usr/bin/modprobe nvidia
   ```

   Prints the approval id (32 hex chars) on stdout. The request records argv, its sha256, your reason and the time.

2. **Ask the human with the `clarify` tool.** One message, four lines: the exact command, what it changes, the blast radius, the rollback. Offer approve/deny buttons. Do not paraphrase the command — quote the bytes you requested.

3. **Wait for the tap.** Only after an explicit tap from the *configured* Telegram user does the gateway record approval:

   ```bash
   uv run --no-project scripts/hermes_consent_agent.py approve <id> \
     --telegram-user-id <approver> --update-id <update> --message-id <message>
   ```

   **This is the gateway's command, not yours.** An agent that calls `approve` itself is escalating without consent; nothing downstream can tell the difference (see *Limitations*).

4. **Run it.** `uv run --no-project scripts/hermes_consent_agent.py run <id>` shells out to `sudo -n … hermes-consent-exec run <id>`. Exit 0 means the approved argv ran as root and printed its output. Non-zero means refused — read the `denied (<code>)` line: `no_approval`, `approver_mismatch`, `expired`, `replay`, `argv_hash_mismatch`, `request_tampered`.

5. **Report the evidence.** Quote the audit line for that id from `/var/log/hermes-consent.log` (0600 root — read it through the human's admin shell, or `status <id>` for state) and say what changed. One attempt, one line, allowed or denied.

## What the human sees

A message naming the command, why it is needed, what it touches and how to undo it, then two buttons. Their tap is tied to one approval id, one argv hash and a TTL (10 minutes by default). A re-tap of an old message is worthless: the id is single-use and the TTL is enforced by the root helper, not by you.

## Blast radius

The approval authorises **one argv, as root, once**. Root can do anything — that command is the boundary, so choose the smallest argv that fixes the symptom (`modprobe nvidia`, not `bash -c "…"`). Installing grants exactly one `NOPASSWD` command to one user (`/etc/sudoers.d/90-hermes-consent`); `sudo -n true`, `sudo /bin/sh` and everything else still demand a password.

**Rollback:** `sudo uv run --no-project scripts/install_consent_sudo.py --uninstall` removes the sudoers rule, helper and config, and keeps the audit log. On NixOS set `services.hermesConsent.enable = false` and rebuild.

## Verification

```bash
sudo -n true                                                   # must FAIL: a password is required
uv run --no-project scripts/hermes_consent_agent.py status <id> # approved/used state
# after a successful run, the audit line must exist:
sudo grep '"id": "<id>"' /var/log/hermes-consent.log
# expected: one line with "decision": "allowed" and the argv you requested
uv run --no-project scripts/hermes_consent_agent.py run <id>    # must now FAIL: replay
```

## Pitfalls

- **Editing argv after approval.** The approval covers a hash of the argv. Change one byte and the helper refuses with `argv_hash_mismatch` — re-request, re-ask.
- **`bash -c "…"` as the approved command.** It defeats the point: the human approved a shell, not a fix. Request the real binary.
- **Relative command.** `argv[0]` must be absolute; the helper execs it with no PATH search.
- **TTL 0.** The installer accepts it for tests only; every approval expires immediately.
- **Assuming the log is readable by you.** It is 0600 root by design. Report the id and let the human's admin read the line.
- **NixOS.** The installer refuses there on purpose (`sudoers` is declarative). Use the flake module; the helper path is `/run/current-system/sw/bin/hermes-consent-exec`.

## Limitations

This stops **prompt injection and silent escalation**: an injected instruction cannot produce a root command without a human tap, and every attempt is logged.

It does **not** stop a fully compromised gateway process. The approval record is written by a user-level process running as the same user as the agent, into a directory that user can write. A process that can call `approve` can mint an approval; the helper verifies *what* was approved and *who* the approver claims to be, not that a human actually tapped. Closing that gap needs the approval and the execution to live in different privilege domains (a separate root-side approval agent, or a hardware token), which this design deliberately does not attempt. Treat a host where the agent and gateway share a uid as "consent is advisory against accidents and injection, not against a compromised host".

Also out of scope: the audit log is append-only by convention, not tamper-evident against root; and root can edit the helper.

## Changelog

- 0.1.0 — initial: installer, root helper, agent CLI, audit log, NixOS module.
