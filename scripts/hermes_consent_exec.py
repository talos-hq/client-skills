#!/usr/bin/env python3
"""Root-side half of consent-gated sudo for Hermes agents.

Installed by ``scripts/install_consent_sudo.py`` as
``/usr/local/libexec/hermes-consent-exec`` (root:root 0755). It is the *only*
command the agent's sudoers entry names, and it is the only place where the
approval checks are made, because it is the only thing that runs as root.

Usage (the agent never calls this directly; ``hermes_consent_agent.py run``
does):

    sudo /usr/local/libexec/hermes-consent-exec run <approval-id>

It reads three things:

* ``/etc/hermes-consent/config.json`` — trusted, root-owned: the Telegram user
  id allowed to approve, the approval TTL, and the agent user.
* ``~<agent>/.hermes/consent/<id>.json`` — the request: argv, sha256(argv),
  reason, requested_at.
* ``~<agent>/.hermes/consent/<id>.approved`` — the approval written by the
  gateway after the human tapped a Telegram button: approver id, update_id,
  message_id, approved_at, sha256(argv).

Every check is made here, after sudo has already granted root, then the argv is
exec'd once. Every attempt — allowed or denied — appends one JSON line to
``/var/log/hermes-consent.log``.

This file must stay dependency-free (stdlib only) and runnable on the stock
``python3`` of the host. It is copied verbatim by the installer.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import stat
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

CONFIG_PATH = Path("/etc/hermes-consent/config.json")
STATE_DIR = Path("/var/lib/hermes-consent")
LOG_PATH = Path("/var/log/hermes-consent.log")

ID_RE = re.compile(r"^[0-9a-f]{32}$")
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ENV_ALLOWLIST = ("TERM", "LANG", "LC_ALL")
MAX_TTL_MINUTES = 24 * 60


def argv_digest(argv: Sequence[str]) -> str:
    """Hash the exact argv list, unambiguously (NUL-separated)."""
    return hashlib.sha256(b"\0".join(arg.encode("utf-8") for arg in argv)).hexdigest()


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_instant(value: Any) -> datetime | None:
    """Parse an ISO-8601 instant; naive values are taken as UTC. None if bad."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def read_json_object(path: Path) -> dict[str, Any] | None:
    """Read a JSON object from a regular file, refusing symlinks and odd types.

    The request and approval files live in a directory the agent can write, so
    a symlink there must never be followed by a root process.
    """
    try:
        info = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None
    if info.st_size > 64 * 1024:
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded


def append_log(record: dict[str, Any]) -> None:
    """Append one JSON line per attempt. Never raises into the decision path."""
    line = json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n"
    try:
        descriptor = os.open(
            LOG_PATH,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            0o600,
        )
        try:
            os.write(descriptor, line.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:  # a broken log must not become a privilege hole
        print(f"hermes-consent: could not write audit log: {error}", file=sys.stderr)


def deny(code: str, message: str, context: dict[str, Any]) -> NoReturn:
    record = {
        "ts": utc_now().isoformat(timespec="seconds"),
        "event": "consent_exec",
        "decision": "denied",
        "reason": code,
        "message": message,
        **context,
    }
    append_log(record)
    print(f"hermes-consent: denied ({code}): {message}", file=sys.stderr)
    raise SystemExit(1)


def allow(context: dict[str, Any]) -> None:
    record = {
        "ts": utc_now().isoformat(timespec="seconds"),
        "event": "consent_exec",
        "decision": "allowed",
        "reason": "ok",
        "message": "argv executed as root",
        **context,
    }
    append_log(record)


def load_config() -> dict[str, Any]:
    config = read_json_object(CONFIG_PATH)
    if config is None:
        deny(
            "bad_config",
            f"cannot read {CONFIG_PATH} (run the installer, or check its mode)",
            {},
        )
    allowed_id = config.get("telegram_user_id")
    ttl = config.get("ttl_minutes")
    agent_user = config.get("agent_user")
    if not isinstance(allowed_id, int) or isinstance(allowed_id, bool):
        deny(
            "bad_config",
            "telegram_user_id is not an integer",
            {"config": str(CONFIG_PATH)},
        )
    if (
        not isinstance(ttl, int | float)
        or isinstance(ttl, bool)
        or not 0 <= float(ttl) <= MAX_TTL_MINUTES
    ):
        deny(
            "bad_config",
            "ttl_minutes is missing or out of range",
            {"config": str(CONFIG_PATH)},
        )
    if not isinstance(agent_user, str) or not agent_user:
        deny("bad_config", "agent_user is missing", {"config": str(CONFIG_PATH)})
    return config


def agent_home(agent_user: str) -> Path:
    try:
        return Path(pwd.getpwnam(agent_user).pw_dir)
    except KeyError:
        deny("bad_config", f"agent_user {agent_user!r} does not exist", {})


def claim(state_dir: Path, approval_id: str) -> bool:
    """Atomically mark an approval used. False means it was already used."""
    marker = state_dir / f"{approval_id}.used"
    try:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"used_at": utc_now().isoformat(timespec="seconds")}) + "\n"
        )
    return True


def safe_environment(agent_user: str, home: Path) -> dict[str, str]:
    """A root environment built from scratch, not inherited from the agent."""
    environment = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
    environment.update(
        {
            "PATH": SAFE_PATH,
            "HOME": str(home),
            "USER": "root",
            "LOGNAME": "root",
            "SHELL": "/bin/sh",
            "SUDO_USER": agent_user,
        }
    )
    return environment


def check_invocation(argv: list[str]) -> str:
    if os.geteuid() != 0:
        deny("not_root", "this helper must be run through sudo", {})
    if len(argv) != 3 or argv[1] != "run":
        deny(
            "bad_args",
            "usage: hermes-consent-exec run <approval-id>",
            {"argv": argv},
        )
    approval_id = argv[2]
    if not ID_RE.match(approval_id):
        deny(
            "bad_id",
            "approval id must be 32 lowercase hex characters",
            {"id": approval_id},
        )
    return approval_id


def main(argv: list[str]) -> int:
    os.chdir("/")
    approval_id = check_invocation(argv)

    sudo_user = os.environ.get("SUDO_USER", "")
    context: dict[str, Any] = {"id": approval_id, "sudo_user": sudo_user}
    config = load_config()
    agent_user = str(config["agent_user"])
    ttl_minutes = float(config["ttl_minutes"])
    allowed_telegram_id = int(config["telegram_user_id"])

    if sudo_user != agent_user:
        deny(
            "wrong_invoker",
            f"only {agent_user!r} may use this helper, not {sudo_user!r}",
            context,
        )

    home = agent_home(agent_user)
    consent_dir = home / ".hermes" / "consent"
    request = read_json_object(consent_dir / f"{approval_id}.json")
    if request is None:
        deny("no_request", "no readable request for this id", context)

    approval = read_json_object(consent_dir / f"{approval_id}.approved")
    if approval is None:
        deny("no_approval", "this request has not been approved", context)

    command = request.get("argv")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) and part for part in command)
    ):
        deny("bad_request", "request argv is missing or malformed", context)
    command = [str(part) for part in command]

    request_digest = argv_digest(command)
    context.update(
        {
            "argv": command,
            "argv_sha256": request_digest,
            "approver_telegram_user_id": approval.get("approver_telegram_user_id"),
            "update_id": approval.get("update_id"),
            "message_id": approval.get("message_id"),
        }
    )

    if request.get("argv_sha256") != request_digest:
        deny("request_tampered", "request argv does not match its own digest", context)

    approver = approval.get("approver_telegram_user_id")
    if not isinstance(approver, int) or isinstance(approver, bool):
        deny("approver_mismatch", "approval has no numeric approver id", context)
    if approver != allowed_telegram_id:
        deny(
            "approver_mismatch",
            f"approver {approver} is not the configured "
            f"Telegram user {allowed_telegram_id}",
            context,
        )
    for field in ("update_id", "message_id"):
        value = approval.get(field)
        if not isinstance(value, int) or isinstance(value, bool):
            deny("bad_approval", f"approval has no numeric {field}", context)

    approved_at = parse_instant(approval.get("approved_at"))
    if approved_at is None:
        deny("bad_approval", "approval has no parseable approved_at", context)
    if utc_now() >= approved_at + timedelta(minutes=ttl_minutes):
        deny(
            "expired",
            f"approval expired (ttl {config['ttl_minutes']} minute(s))",
            context,
        )

    if approval.get("argv_sha256") != request_digest:
        deny(
            "argv_hash_mismatch",
            "approval covers different argv than the request",
            context,
        )
    if approval.get("id") not in (None, approval_id):
        deny("bad_approval", "approval belongs to a different id", context)

    try:
        STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(STATE_DIR, 0o700)
    except OSError as error:
        deny("state_error", f"cannot prepare {STATE_DIR}: {error}", context)

    if not claim(STATE_DIR, approval_id):
        deny("replay", "this approval has already been used", context)

    allow(context)
    try:
        os.execve(command[0], command, safe_environment(agent_user, home))
    except OSError as error:
        deny("exec_error", f"cannot exec {command[0]!r}: {error}", context)
    return 1  # unreachable: execve replaces the process


if __name__ == "__main__":
    sys.exit(main(sys.argv))
