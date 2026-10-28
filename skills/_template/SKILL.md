---
name: example-skill
description: "Use when <the symptom>. <what this does>."
version: 0.1.0
author: Talos HQ
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [client, example]
    category: client
---

# <Skill title>

<One sentence: what state this leaves the machine in.>

## When to use

- The symptom the agent will actually see (exact error text where possible).
- What must be true before starting (a working shell, a specific service, network).

**Do not use when** <the case that looks the same but needs the other skill>.

## Prerequisites

| Need | How to check |
|---|---|
| <tool/permission> | `<command that proves it>` |

## Procedure

1. **Check the current state.** `<command>` — expected output before any change.
2. **<Do the thing.>** `<command>` — say what it changes and how to undo it.
3. **Verify.** `<command>` — the output that proves success, not the exit code alone.

Anything that escalates privilege, deletes data or restarts a service states its blast radius first:
what it touches, who is affected, and the exact rollback.

## Verification

```bash
<command>
# expected:
<expected output>
```

## Pitfalls

- **<Trap>** — what it looks like, and the correct response.
- **<Headless case>** — what fails when there is no TTY or session bus, and what to hand the user instead.

## References

- [references/<topic>.md](references/<topic>.md) — <what depth lives there>

## Changelog

- 0.1.0 — initial.
