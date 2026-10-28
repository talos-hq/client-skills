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

## Versioning

- Bump `version` in the skill's frontmatter for any behavioural change; note the change in the skill's own *Changelog* section at the bottom.
- Tag releases `skill-<name>-v<version>` so a client can pin a known-good skill:
  `https://raw.githubusercontent.com/talos-hq/client-skills/skill-<name>-v<version>/skills/<name>/SKILL.md`.
- A skill may not be deleted while a client pins it: mark it `deprecated: true` in its frontmatter for one release cycle first.

## Adding a skill

1. `cp -r skills/_template skills/<your-skill>`, rename and edit.
2. Run the validator.
3. Open a PR describing **who** the skill is for (which client, which install shape), **what state** it leaves the machine in, and **how it was tested** — a dry run on a disposable host or container.
