---
name: hermes-gateway-triage
description: "Use when a Hermes bot goes silent. Triage gateway state."
version: 0.1.0
author: Talos HQ
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [client, gateway, telegram, triage]
    category: client
    related_skills: []
---

# Hermes gateway triage

Finds out why a bot stopped answering and leaves the gateway owned by exactly one process, serving every profile.

## When to use

- The bot is silent: messages arrive in the chat but nothing answers.
- `systemctl --user status hermes-gateway` shows `activating (auto-restart)` and a rising `NRestarts`.
- A new profile's bot never comes online although its token is valid.

**Do not use when** the model provider is failing (a 403/quota error still produces replies or errors in the chat). This skill is about the process, not the provider.

## Prerequisites

| Need | How to check |
|---|---|
| Access to the host's user session | `systemctl --user is-active hermes-gateway` |
| The bot token for the profile | `grep TELEGRAM_BOT_TOKEN ~/dev/hermes/profiles/<profile>/.env` |

## Procedure

1. **Is the unit healthy, or looping?**

   ```bash
   systemctl --user is-active hermes-gateway
   systemctl --user show hermes-gateway -p NRestarts -p MainPID --value
   ```

   `active` with a stable `NRestarts` means the unit is fine — skip to step 3.
   `activating (auto-restart)` with a high count is the singleton fight: read the journal next.

   ```bash
   journalctl --user -u hermes-gateway --since "-10 min" | grep -i "one gateway per host"
   ```

   That message means another gateway already owns the host's lock (`~/dev/hermes/gateway.pid`).

2. **Who owns the lock?** Compare the PID file with the unit's `MainPID`.

   ```bash
   cat ~/dev/hermes/gateway.pid
   cat /proc/<pid>/cgroup | head -1
   ```

   A cgroup like `app-hermes-*.scope` means the process was spawned by the **desktop app**, not by systemd. Only one of them may run: stop the stray process, then `systemctl --user restart hermes-gateway` so the unit owns the gateway. One gateway per host serves every profile — never add a per-profile unit.

3. **Does the profile's bot actually have a channel?** A cloned profile has its token but no channels: `hermes profile create --clone` deliberately strips them.

   ```bash
   grep -A3 "^platforms:" ~/dev/hermes/profiles/<profile>/config.yaml
   grep -A1 "^gateway:" ~/dev/hermes/profiles/<profile>/config.yaml
   ```

   Both must be present (`platforms.telegram.enabled: true` and `gateway.multiplex_profiles: true`). The gateway rescans profiles and starts the new adapter without a restart.

4. **Is anything actually polling the bot?** This is the decisive check; the answer is in the HTTP status.

   ```bash
   set -a; . ~/dev/hermes/profiles/<profile>/.env; set +a
   curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates?timeout=0&limit=1"
   ```

   - `409 Conflict, terminated by other getUpdates request` — **healthy**: a gateway is long-polling it.
   - `200 {"ok":true,"result":[]}` — nothing is polling it: the adapter never started, so go back to step 3.

## Verification

```bash
systemctl --user is-active hermes-gateway        # active
systemctl --user show hermes-gateway -p NRestarts --value   # 0, and not climbing
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates?timeout=0" | head -c 120
# expected: 409 conflict — a poller exists
```

Then send one message from the user's account and confirm a reply.

## Pitfalls

- **`409` can be your own probe.** Two concurrent `getUpdates` calls conflict; run the probe once, not in a loop.
- **The desktop app respawns its gateway on launch.** After fixing the fight, disable gateway autostart in the desktop app or keep it off that host, or the loop returns on the next launch.
- **A valid token is not a working bot.** Token validity (`getMe`) says nothing about whether a channel is declared or polled.
- **Do not restart the gateway from inside a gateway-hosted session.** The agent's own turn dies with it; schedule the restart detached (`systemd-run --user --collect --unit=<name> <command>`) or hand it to the user.

## Changelog

- 0.1.0 — initial, from the multiplexed-gateway incident on a NixOS host.
