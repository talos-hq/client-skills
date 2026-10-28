#!/usr/bin/env python3
"""Agent-side CLI for consent-gated sudo.

The agent uses three commands:

    hermes_consent_agent.py request --reason "graphics driver is wedged" -- id -u
    hermes_consent_agent.py run <id>
    hermes_consent_agent.py status <id>

``approve`` exists for the *human-approval step* — the gateway, after the
person taps the Telegram button. It is not an agent command: an agent that
calls ``approve`` itself is escalating without consent, and the record it
writes is indistinguishable from a real one (see the skill's Limitations).
The root helper treats the approval file as untrusted input and checks the
approver id, the TTL and the argv hash regardless.

Nothing here runs as root. ``run`` shells out to the single sudoers-approved
helper and reports its exit code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Overridable because NixOS puts the helper on the system path instead of
# /usr/local/libexec. The override cannot widen privilege: sudo still matches
# the literal path against the one rule in sudoers, so pointing it elsewhere
# only gets a "password is required" refusal.
HELPER_PATH = Path(
    os.environ.get("HERMES_CONSENT_HELPER", "/usr/local/libexec/hermes-consent-exec")
)
CONFIG_PATH = Path("/etc/hermes-consent/config.json")
STATE_DIR = Path("/var/lib/hermes-consent")
ID_RE = re.compile(r"^[0-9a-f]{32}$")


def argv_digest(argv: Sequence[str]) -> str:
    """Must match hermes_consent_exec.argv_digest byte for byte."""
    return hashlib.sha256(b"\0".join(arg.encode("utf-8") for arg in argv)).hexdigest()


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def consent_dir() -> Path:
    return Path.home() / ".hermes" / "consent"


def fail(message: str, code: int = 1) -> int:
    print(f"hermes-consent: error: {message}", file=sys.stderr)
    return code


def check_id(approval_id: str) -> str | None:
    if not ID_RE.match(approval_id):
        return "approval id must be 32 lowercase hex characters"
    return None


def write_json(path: Path, payload: dict[str, Any], mode: int = 0o600) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def split_command(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split sys.argv at the first bare ``--``: options before, argv after."""
    if "--" not in argv:
        return argv, []
    index = argv.index("--")
    return argv[:index], argv[index + 1 :]


def command_request(argv: list[str]) -> int:
    head, command = split_command(argv)
    parser = argparse.ArgumentParser(
        prog="hermes_consent_agent.py request",
        description="Record a root command you want a human to approve.",
    )
    parser.add_argument("--reason", required=True, help="one line the human will read")
    args = parser.parse_args(head)
    if not command:
        return fail("nothing to approve: put the command after `--`")
    if any(not part for part in command):
        return fail("argv contains an empty argument")
    if not command[0].startswith("/"):
        return fail(
            "argv[0] must be an absolute path (the helper execs it, no PATH search)"
        )
    if not args.reason.strip():
        return fail("--reason must not be empty")

    directory = consent_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    approval_id = secrets.token_hex(16)
    payload: dict[str, Any] = {
        "id": approval_id,
        "argv": command,
        "argv_sha256": argv_digest(command),
        "reason": args.reason.strip(),
        "requested_at": now_iso(),
    }
    write_json(directory / f"{approval_id}.json", payload)
    print(approval_id)
    print(f"reason: {payload['reason']}", file=sys.stderr)
    print(f"command: {command!r}", file=sys.stderr)
    print("approval needed from the configured Telegram user", file=sys.stderr)
    return 0


def command_approve(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="hermes_consent_agent.py approve",
        description="Record a human's Telegram tap (gateway, not the agent).",
    )
    parser.add_argument("approval_id")
    parser.add_argument("--telegram-user-id", type=int, required=True)
    parser.add_argument("--update-id", type=int, required=True)
    parser.add_argument("--message-id", type=int, required=True)
    args = parser.parse_args(argv)

    problem = check_id(args.approval_id)
    if problem:
        return fail(problem)
    directory = consent_dir()
    request = read_json(directory / f"{args.approval_id}.json")
    if request is None:
        return fail(f"no request {args.approval_id}; run `request` first")
    command = request.get("argv")
    if not isinstance(command, list) or not all(
        isinstance(item, str) for item in command
    ):
        return fail("request is malformed; make a new one")

    payload: dict[str, Any] = {
        "id": args.approval_id,
        "approver_telegram_user_id": args.telegram_user_id,
        "update_id": args.update_id,
        "message_id": args.message_id,
        "approved_at": now_iso(),
        "argv_sha256": argv_digest([str(item) for item in command]),
    }
    write_json(directory / f"{args.approval_id}.approved", payload)
    print(f"approved {args.approval_id} by telegram user {args.telegram_user_id}")
    return 0


def command_run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="hermes_consent_agent.py run",
        description="Ask the root helper to execute an approved request.",
    )
    parser.add_argument("approval_id")
    args = parser.parse_args(argv)

    problem = check_id(args.approval_id)
    if problem:
        return fail(problem)
    if not HELPER_PATH.exists():
        return fail(f"{HELPER_PATH} is not installed; run install_consent_sudo.py")

    completed = subprocess.run(
        ["sudo", "-n", str(HELPER_PATH), "run", args.approval_id],
        check=False,
    )
    if completed.returncode != 0:
        return fail(f"helper exited {completed.returncode} for {args.approval_id}")
    return 0


def command_status(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="hermes_consent_agent.py status")
    parser.add_argument("approval_id")
    args = parser.parse_args(argv)

    problem = check_id(args.approval_id)
    if problem:
        return fail(problem)
    directory = consent_dir()
    request = read_json(directory / f"{args.approval_id}.json")
    if request is None:
        return fail(f"no request {args.approval_id}")
    approval = read_json(directory / f"{args.approval_id}.approved")
    marker = STATE_DIR / f"{args.approval_id}.used"
    try:
        used = marker.exists()
    except OSError:
        used = False
    report: dict[str, Any] = {
        "id": args.approval_id,
        "request": request,
        "approved": approval is not None,
        "approval": approval,
        "used": used,
        "ttl_minutes": None,
    }
    config = read_json(CONFIG_PATH)
    if config is not None:
        report["ttl_minutes"] = config.get("ttl_minutes")
        report["telegram_user_id"] = config.get("telegram_user_id")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def command_list(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="hermes_consent_agent.py list")
    parser.add_argument("--json", action="store_true", help="emit one JSON array")
    args = parser.parse_args(argv)

    directory = consent_dir()
    entries: list[dict[str, Any]] = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            request = read_json(path)
            if request is None:
                continue
            entries.append(
                {
                    "id": request.get("id", path.stem),
                    "requested_at": request.get("requested_at"),
                    "reason": request.get("reason"),
                    "argv": request.get("argv"),
                    "approved": (directory / f"{path.stem}.approved").exists(),
                }
            )
    entries.sort(key=lambda entry: str(entry.get("requested_at") or ""))
    if args.json:
        print(json.dumps(entries, indent=2, sort_keys=True))
        return 0
    if not entries:
        print("no pending requests")
        return 0
    for entry in entries:
        state = "approved" if entry["approved"] else "pending"
        print(
            f"{entry['id']}  {state:8}  {entry['requested_at']}  "
            f"{entry['reason']}  {entry['argv']!r}"
        )
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    handlers = {
        "request": command_request,
        "approve": command_approve,
        "run": command_run,
        "status": command_status,
        "list": command_list,
    }
    handler = handlers.get(argv[1])
    if handler is None:
        return fail(f"unknown command {argv[1]!r}; one of {sorted(handlers)}", 2)
    return handler(argv[2:])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
