#!/usr/bin/env python3
"""Install consent-gated sudo for a Hermes agent.

Grants exactly one command to one user:

    <agent> ALL=(root) NOPASSWD: /usr/local/libexec/hermes-consent-exec

That helper refuses to run anything until a human has approved the exact argv
in Telegram, refuses approvals from anyone but the configured Telegram user,
refuses stale approvals, refuses replays, and logs every attempt.

Run it as root:

    sudo uv run --no-project scripts/install_consent_sudo.py \\
        --telegram-user-id 123456789

    sudo uv run --no-project scripts/install_consent_sudo.py --uninstall

It never edits /etc/sudoers. It writes /etc/sudoers.d/90-hermes-consent only
after validating the content with ``visudo -cf``, and swaps it in atomically.
Re-running it is safe: every file is rewritten in place, nothing is duplicated.
"""

from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn

HELPER_PATH = Path("/usr/local/libexec/hermes-consent-exec")
CONFIG_DIR = Path("/etc/hermes-consent")
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_DIR = Path("/var/lib/hermes-consent")
LOG_PATH = Path("/var/log/hermes-consent.log")
SUDOERS_PATH = Path("/etc/sudoers.d/90-hermes-consent")
MAX_TTL_MINUTES = 24 * 60

NIXOS_NOTE = """\
Refusing: NixOS manages /etc/sudoers declaratively, so a file dropped into
/etc/sudoers.d is either ignored or reverted on the next rebuild. Add the rule
to your configuration instead, and point it at the helper this installer would
have written (run the installer on a non-NixOS host to obtain it):

  security.sudo.extraRules = [{
    users = [ "%(agent)s" ];
    commands = [{
      command = "/run/current-system/sw/bin/hermes-consent-exec";
      options = [ "NOPASSWD" ];
    }];
  }];

Do not hand-write this: the repository ships a tested module that declares the
rule, the helper, /etc/hermes-consent/config.json and the state/log tmpfiles —
`nix/consent-sudo.nix`. Import it, set its options, and rebuild
(`sudo nixos-rebuild switch`).
"""


def fail(message: str, code: int = 1) -> NoReturn:
    print(f"install-consent-sudo: error: {message}", file=sys.stderr)
    raise SystemExit(code)


def os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip().strip("\"'")
    return values


def is_nixos() -> bool:
    release = os_release()
    return (
        release.get("ID", "").lower() == "nixos"
        or "nixos" in release.get("ID_LIKE", "").lower().split()
        or Path("/etc/NIXOS").exists()
    )


def resolve_agent_user(explicit: str | None) -> str:
    candidate = explicit or os.environ.get("SUDO_USER", "")
    if not candidate:
        fail("cannot tell which user should get the sudo rule; pass --agent-user")
    if candidate == "root":
        fail(
            "the agent user must not be root; this grants root, it does not run as root"
        )
    try:
        return pwd.getpwnam(candidate).pw_name
    except KeyError:
        fail(f"user {candidate!r} does not exist on this host")


def owner_ids(user: str, group: str) -> tuple[int, int]:
    try:
        return pwd.getpwnam(user).pw_uid, grp.getgrnam(group).gr_gid
    except KeyError as error:
        fail(f"cannot resolve owner {user}:{group}: {error}")


def ensure_dir(path: Path, mode: int, user: str, group: str) -> None:
    if path.is_symlink():
        fail(f"{path} is a symlink; refusing to use it")
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    uid, gid = owner_ids(user, group)
    os.chown(path, uid, gid)
    os.chmod(path, mode)


def write_file_atomic(
    path: Path, data: bytes, mode: int, user: str, group: str
) -> None:
    """Write bytes to a sibling temp file, fsync, then rename over the target."""
    uid, gid = owner_ids(user, group)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}."
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temporary_path, uid, gid)
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def tools_available() -> None:
    for tool in ("visudo",):
        if shutil.which(tool) is None:
            fail(f"{tool} not found; install sudo before running this installer")


def helper_source() -> bytes:
    source = Path(__file__).resolve().parent / "hermes_consent_exec.py"
    if not source.is_file():
        fail(f"helper source missing at {source}; run the installer from a checkout")
    text = source.read_text(encoding="utf-8")
    if not text.startswith("#!"):
        fail(f"{source} has no shebang; it must be directly executable by sudo")
    return text.encode("utf-8")


def sudoers_content(agent_user: str) -> str:
    return (
        "# Managed by install_consent_sudo.py — re-run it instead of editing.\n"
        "# Grants exactly one command; every escalation is Telegram-approved.\n"
        f"{agent_user} ALL=(root) NOPASSWD: {HELPER_PATH}\n"
    )


def install_sudoers(agent_user: str) -> None:
    content = sudoers_content(agent_user).encode("utf-8")
    SUDOERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Dot-prefixed temp name: sudo's #includedir ignores names containing '.',
    # so a half-written file can never break sudo for the whole host.
    descriptor, temporary = tempfile.mkstemp(
        dir=str(SUDOERS_PATH.parent), prefix=f".{SUDOERS_PATH.name}."
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o440)
        result = subprocess.run(
            ["visudo", "-cf", str(temporary_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            fail(f"visudo rejected the generated rule: {result.stderr.strip()}")
        uid, gid = owner_ids("root", "root")
        os.chown(temporary_path, uid, gid)
        os.replace(temporary_path, SUDOERS_PATH)
        os.chmod(SUDOERS_PATH, 0o440)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def config_payload(agent_user: str, telegram_user_id: int, ttl_minutes: int) -> bytes:
    payload: dict[str, Any] = {
        "agent_user": agent_user,
        "telegram_user_id": telegram_user_id,
        "ttl_minutes": ttl_minutes,
    }
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def install(args: argparse.Namespace, agent_user: str) -> int:
    tools_available()
    helper = helper_source()

    ensure_dir(HELPER_PATH.parent, 0o755, "root", "root")
    write_file_atomic(HELPER_PATH, helper, 0o755, "root", "root")

    ensure_dir(CONFIG_DIR, 0o755, "root", "root")
    write_file_atomic(
        CONFIG_PATH,
        config_payload(agent_user, args.telegram_user_id, args.ttl_minutes),
        0o644,
        "root",
        "root",
    )

    ensure_dir(STATE_DIR, 0o700, "root", "root")
    if not LOG_PATH.exists():
        write_file_atomic(LOG_PATH, b"", 0o600, "root", "root")
    else:
        os.chmod(LOG_PATH, 0o600)

    account = pwd.getpwnam(agent_user)
    consent_dir = Path(account.pw_dir) / ".hermes" / "consent"
    ensure_dir(consent_dir, 0o700, "root", "root")
    os.chown(consent_dir, account.pw_uid, account.pw_gid)
    os.chmod(consent_dir, 0o700)
    home = Path(account.pw_dir)

    # Last: the rule is only worth writing once the thing it points at exists.
    install_sudoers(agent_user)

    print(f"installed helper    {HELPER_PATH} (root:root 0755)")
    print(f"wrote config        {CONFIG_PATH} (root:root 0644)")
    print(f"wrote sudoers rule  {SUDOERS_PATH} (root:root 0440)")
    print(f"state + audit       {STATE_DIR}, {LOG_PATH}")
    print(f"approvals           {home / '.hermes' / 'consent'} (owner {agent_user})")
    print(f"agent user          {agent_user}")
    if args.ttl_minutes == 0:
        print("warning: ttl 0 — every approval expires immediately (testing only)")
    print(f"next: {agent_user} runs `sudo -n {HELPER_PATH} run <id>` after approval")
    return 0


def uninstall() -> int:
    removed: list[Path] = []
    for path in (SUDOERS_PATH, HELPER_PATH, CONFIG_PATH):
        if path.exists() or path.is_symlink():
            path.unlink()
            removed.append(path)
    for directory in (CONFIG_DIR, HELPER_PATH.parent):
        try:
            directory.rmdir()
            removed.append(directory)
        except OSError:
            pass
    for path in removed:
        print(f"removed {path}")
    if not removed:
        print("nothing to remove; consent-gated sudo is not installed")
    print(
        f"kept {LOG_PATH} and {STATE_DIR} (audit trail; delete by hand if you mean to)"
    )
    print("run `sudo -n true` as the agent user: it must fail")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install Telegram-approved (consent-gated) sudo for an agent.",
    )
    parser.add_argument(
        "--telegram-user-id",
        type=int,
        help="Telegram user id allowed to approve escalations (required to install)",
    )
    parser.add_argument(
        "--ttl-minutes",
        type=int,
        default=10,
        help="how long an approval stays valid (default: 10)",
    )
    parser.add_argument(
        "--agent-user",
        default=None,
        help="user that receives the sudo rule (default: $SUDO_USER)",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="remove the sudoers rule, helper and config; keep the audit log",
    )
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv[1:])

    if os.geteuid() != 0:
        fail(
            "must run as root, e.g. `sudo uv run --no-project "
            "scripts/install_consent_sudo.py ...`"
        )
    if is_nixos():
        print(NIXOS_NOTE % {"agent": args.agent_user or "$SUDO_USER"}, file=sys.stderr)
        return 2
    if args.uninstall:
        return uninstall()

    if args.telegram_user_id is None:
        fail(
            "--telegram-user-id is required (the only Telegram user allowed to approve)"
        )
    if args.telegram_user_id <= 0:
        fail("--telegram-user-id must be a positive integer")
    if not 0 <= args.ttl_minutes <= MAX_TTL_MINUTES:
        fail(f"--ttl-minutes must be between 0 and {MAX_TTL_MINUTES}")

    agent_user = resolve_agent_user(args.agent_user)
    return install(args, agent_user)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
