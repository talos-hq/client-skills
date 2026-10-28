# Client skills

Hermes Agent skills that Talos HQ hands to clients — one directory per skill, versioned in git, validated before anything ships.

A skill in this repo is a **provisioning procedure, not just documentation**: the agent reads it and acts. The bar is that a client's agent can follow it unattended and end in a verifiable state.

## Install

```bash
hermes skills install talos-hq/client-skills/<skill-name>
```

A skill whose `SKILL.md` lives on a tag or branch can also be installed by URL:

```bash
hermes skills install https://raw.githubusercontent.com/talos-hq/client-skills/main/skills/<skill-name>/SKILL.md
```

Clients pin by tagging: a skill's frontmatter `version` and the repo tag must agree (see *Versioning* below).

## Layout

```
skills/<skill-name>/SKILL.md          the skill itself — required
skills/<skill-name>/references/*.md   depth that would bloat SKILL.md (optional)
skills/<skill-name>/scripts/*         helper scripts the skill calls (optional)
scripts/validate_skills.py            the gate this repo runs on every PR
scripts/install_consent_sudo.py       consent-gated sudo installer (see below)
bosn.toml, bosn/*.Dockerfile          container targets for the e2e suite
nix/, tests/nixos/                    NixOS module and VM test for the same policy
tests/e2e/                            the container e2e suite
```

Directory name **is** the skill name: `skills/fix-dns/SKILL.md` has `name: fix-dns`.

## Rules

- **Description is a trigger, not a title.** `Use when <trigger>. <what it does>.`, at most 60 characters — it is what the agent matches on when deciding to load the skill.
- **No secrets, no client-specific values.** Hostnames, tokens, paths and ports are parameters the skill asks for. A credential in this repo is a leak, and the validator fails on obvious key material.
- **Say the blast radius.** Any step that escalates privilege, deletes data, reboots or changes network access must state what it affects and how to undo it before the command, not after.
- **Every skill ends with a verification section.** "It should work" is not a state; a command whose output proves it is.
- **SKILL.md stays short.** Anything longer than a screenful belongs in `references/`, linked by relative path.
- **Write the failure mode first.** The agent reads the skill when something is already broken; start from the symptom it will see.

## Validation

```bash
uv run --no-project scripts/validate_skills.py          # all skills
uv run --no-project scripts/validate_skills.py --strict # treat warnings as errors
```

CI runs the same command on every push and pull request that touches `skills/`. Directories starting with `_` (such as `_template`) are skipped.

The Python in `scripts/` and `tests/` is checked with pinned tools (config in `pyproject.toml`):

```bash
uvx ruff@0.12.10 format --check .
uvx ruff@0.12.10 check .
uvx basedpyright@1.40.2
```

## Consent-gated sudo

An agent has to be able to fix a wedged machine (a broken graphics driver, a
package that will not repair) that needs root — without being handed blanket
root. Every escalation is a Telegram approval the human taps, and every attempt
is logged.

**What it grants:** exactly one command to one user.

```
<agent-user> ALL=(root) NOPASSWD: /usr/local/libexec/hermes-consent-exec
```

No argument wildcards. That helper refuses unless the exact argv was approved by
the configured Telegram user, within the TTL, once; it records one JSON line per
attempt (allowed *and* denied) in `/var/log/hermes-consent.log` (0600 root). The
generated sudoers fragment is validated with `visudo -cf` before it is swapped
in atomically; `/etc/sudoers` itself is never edited. The agent-side procedure is
in [`skills/hermes-sudo-consent/SKILL.md`](skills/hermes-sudo-consent/SKILL.md).

The same policy is delivered three ways — pick the row that matches the host:

| Target | What runs | Command |
|---|---|---|
| Ubuntu (LTS) | installer + `sudoers.d`, e2e in a `bosn` container | `bosn run --task e2e-ubuntu` |
| Arch Linux | the *same* installer, unchanged (pacman host, no apt) | `bosn run --task e2e-arch` |
| NixOS | **the flake attribute replaces the installer**: sudoers is declarative, so the module declares the rule, the helper, `/etc/hermes-consent/config.json` and the tmpfiles | `nixos-rebuild switch` with the module below; verify with `nix build .#checks.x86_64-linux.consent-sudo` |

### Install (Ubuntu, Arch, any non-NixOS host)

```bash
sudo uv run --no-project scripts/install_consent_sudo.py \
  --telegram-user-id <telegram-user-id> [--ttl-minutes 10] [--agent-user <user>]
```

`--agent-user` defaults to `$SUDO_USER`. Re-running is safe; `--uninstall`
removes the rule, helper and config and keeps the audit log. On NixOS the
installer refuses and prints the `security.sudo.extraRules` alternative — use
the module instead:

```nix
imports = [ (builtins.getFlake "<this-repo>").nixosModules.consent-sudo ];
services.hermesConsent = {
  enable = true;
  agentUser = "hermes";          # must not be root
  telegramUserId = 123456789;    # the only approver
  ttlMinutes = 10;
};
```

The rule names `/run/current-system/sw/bin/hermes-consent-exec`; set
`HERMES_CONSENT_HELPER` to that path for the agent CLI.

### Test

```bash
bosn run --task e2e-ubuntu   # Ubuntu 24.04: install, approvals, denials, audit, uninstall
bosn run --task e2e-arch     # Arch Linux: same suite, same installer
bosn run --task e2e-all      # every assertion on the default stack + the skill gate
```

Each task exits non-zero on the first failed assertion and prints one `PASS` /
`FAIL` / `SKIP <reason>` line per check. A check a distro genuinely cannot
support is reported as `SKIP` with its reason — never silently passed.

Raw Docker fallback (no `bosn`):

```bash
docker build -f bosn/ubuntu.Dockerfile -t hermes-consent-e2e .
docker run --rm -v "$PWD:/repo:ro" hermes-consent-e2e \
  uv run --no-project /repo/tests/e2e/test_consent_sudo.py
```

NixOS, needs Nix + KVM:

```bash
nix build .#checks.x86_64-linux.consent-sudo
```

**Limitation, stated plainly:** this stops prompt injection and silent
escalation. It does **not** stop a fully compromised gateway process — the
approval record is written by a user-level process running as the same user as
the agent, so a process that can call `approve` can mint an approval. See the
skill's *Limitations* section.

## Versioning

- Bump `version` in the skill's frontmatter for any behavioural change; note the change in the skill's own *Changelog* section at the bottom.
- Tag releases `skill-<name>-v<version>` so a client can pin a known-good skill:
  `https://raw.githubusercontent.com/talos-hq/client-skills/skill-<name>-v<version>/skills/<name>/SKILL.md`.
- A skill may not be deleted while a client pins it: mark it `deprecated: true` in its frontmatter for one release cycle first.

## Adding a skill

1. `cp -r skills/_template skills/<your-skill>`, rename and edit.
2. Run the validator.
3. Open a PR describing **who** the skill is for (which client, which install shape), **what state** it leaves the machine in, and **how it was tested** — a dry run on a disposable host or container.
