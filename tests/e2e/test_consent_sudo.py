#!/usr/bin/env python3
"""End-to-end proof for consent-gated sudo, inside a throwaway container.

Run it with the image's entrypoint (this is the documented command):

    docker build -f tests/e2e/Dockerfile -t hermes-consent-e2e .
    docker run --rm hermes-consent-e2e

or directly against a disposable host as root:

    sudo python3 tests/e2e/test_consent_sudo.py --phase both

Two phases, because the audit log is root-only by design:

* ``agent`` — runs as the unprivileged user: installs, requests, approves,
  runs, and checks the denials. It can never read the audit log.
* ``audit`` — runs as root afterwards and reads /var/log/hermes-consent.log.

Everything is real: the real installer, the real sudoers rule, real sudo. No
check is relaxed to make a case pass; a failure exits non-zero.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent.parent
INSTALLER = REPO / "scripts" / "install_consent_sudo.py"
AGENT_CLI = REPO / "scripts" / "hermes_consent_agent.py"
HELPER = Path("/usr/local/libexec/hermes-consent-exec")
SUDOERS = Path("/etc/sudoers.d/90-hermes-consent")
CONFIG = Path("/etc/hermes-consent/config.json")
LOG = Path("/var/log/hermes-consent.log")
STATE_DIR = Path("/var/lib/hermes-consent")

TELEGRAM_USER_ID = 424242
WRONG_TELEGRAM_USER_ID = 999999
AGENT_USER = "agent"
AGENT_HOME = Path("/home/agent")
HEX_ID = re.compile(r"^[0-9a-f]{32}$")


class Checks:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    def ok(self, name: str, detail: str = "") -> None:
        self.passed += 1
        print(f"PASS {name}" + (f"  [{detail}]" if detail else ""))

    def bad(self, name: str, detail: str) -> None:
        self.failed += 1
        print(f"FAIL {name}  [{detail}]")

    def skip(self, name: str, reason: str) -> None:
        self.skipped += 1
        print(f"SKIP {name}  [reason: {reason}]")

    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        if condition:
            self.ok(name, detail)
        else:
            self.bad(name, detail)
        return condition

    def summary(self) -> int:
        print(f"\n{self.passed} passed, {self.failed} failed, {self.skipped} skipped")
        return 0 if self.failed == 0 else 1


def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, capture_output=True, text=True, check=False, **kwargs
    )


def distro_name() -> str:
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.partition("=")[2].strip().strip('"')
    except OSError:
        pass
    return "unknown"


def short(completed: subprocess.CompletedProcess[str]) -> str:
    text = (completed.stderr or completed.stdout).strip().splitlines()
    return text[-1] if text else f"exit {completed.returncode}"


def install(checks: Checks, *extra: str) -> subprocess.CompletedProcess[str]:
    # The path is spelled exactly as the harness sudoers rule allows; note the
    # rule grants *only* this script (see tests/e2e/Dockerfile).
    command = [
        "sudo",
        "-n",
        "/usr/bin/python3",
        str(INSTALLER),
        "--telegram-user-id",
        str(TELEGRAM_USER_ID),
    ]
    command.extend(extra)
    completed = run(command)
    checks.check(
        f"installer runs ({' '.join(extra) or 'defaults'})",
        completed.returncode == 0,
        short(completed),
    )
    return completed


def request(checks: Checks, *command: str, reason: str = "e2e: prove the path") -> str:
    completed = run(
        [sys.executable, str(AGENT_CLI), "request", "--reason", reason, "--", *command]
    )
    approval_id = (
        completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else ""
    )
    checks.check(
        f"request {' '.join(command)} returns an id",
        completed.returncode == 0 and bool(HEX_ID.match(approval_id)),
        short(completed) if not HEX_ID.match(approval_id) else approval_id,
    )
    return approval_id


def approve(
    checks: Checks, approval_id: str, telegram_user_id: int, label: str
) -> None:
    completed = run(
        [
            sys.executable,
            str(AGENT_CLI),
            "approve",
            approval_id,
            "--telegram-user-id",
            str(telegram_user_id),
            "--update-id",
            "7001",
            "--message-id",
            "88",
        ]
    )
    checks.check(
        f"approval recorded ({label})", completed.returncode == 0, short(completed)
    )


def run_helper(approval_id: str) -> subprocess.CompletedProcess[str]:
    return run_helper_extra("run", approval_id)


def run_helper_extra(*arguments: str) -> subprocess.CompletedProcess[str]:
    return run(["sudo", "-n", str(HELPER), *arguments])


def agent_phase() -> int:
    checks = Checks()
    if os.geteuid() == 0:
        print("agent phase must not run as root", file=sys.stderr)
        return 2

    print(f"target: {distro_name()}")

    print("\n== a. no sudo before install ==")
    before = run(["sudo", "-n", "true"])
    checks.check(
        "sudo -n true fails before install", before.returncode != 0, short(before)
    )

    print("\n== install ==")
    install(checks)
    checks.check("helper installed", HELPER.is_file(), str(HELPER))
    checks.check("config installed", CONFIG.is_file(), str(CONFIG))
    # /etc/sudoers.d is 0755 on Debian-family images and 0750 on Arch, where the
    # unprivileged phase cannot even stat the fragment. Both cases are honest:
    # the root audit phase checks the content and mode either way.
    try:
        fragment = SUDOERS.stat()
        visible = fragment.st_size > 0
        invisible_reason = ""
    except OSError as error:
        visible = False
        invisible_reason = str(error)
    if invisible_reason:
        checks.skip(
            "sudoers fragment readable by the agent",
            f"/etc/sudoers.d is root-only here ({invisible_reason}); "
            "the root audit phase verifies the rule",
        )
    else:
        checks.check("sudoers fragment installed", visible, str(SUDOERS))
    again = install(checks)
    checks.check("installer is idempotent", again.returncode == 0, short(again))

    print("\n== b. still no blanket sudo ==")
    after = run(["sudo", "-n", "true"])
    checks.check(
        "sudo -n true still fails after install", after.returncode != 0, short(after)
    )
    shell = run(["sudo", "-n", "/bin/sh", "-c", "id -u"])
    checks.check("sudo /bin/sh is refused", shell.returncode != 0, short(shell))
    bare = run(["sudo", "-n", "/usr/bin/id", "-u"])
    checks.check("sudo /usr/bin/id is refused", bare.returncode != 0, short(bare))
    extra = run_helper_extra("run", "deadbeefdeadbeefdeadbeefdeadbeef", "extra")
    checks.check(
        "helper rejects extra arguments",
        extra.returncode != 0 and "bad_args" in extra.stderr,
        short(extra),
    )

    print("\n== c. unapproved ids are refused ==")
    ghost = "deadbeefdeadbeefdeadbeefdeadbeef"
    missing = run_helper(ghost)
    checks.check(
        "unapproved id refused",
        missing.returncode != 0 and "no_request" in missing.stderr,
        short(missing),
    )
    malformed = run_helper("not-an-id")
    checks.check(
        "malformed id refused",
        malformed.returncode != 0 and "bad_id" in malformed.stderr,
        short(malformed),
    )
    pending = request(checks, "/usr/bin/id", "-u", reason="e2e: unapproved")
    not_yet = run_helper(pending)
    checks.check(
        "request without approval refused",
        not_yet.returncode != 0 and "no_approval" in not_yet.stderr,
        short(not_yet),
    )

    print("\n== d. the wrong Telegram user cannot approve ==")
    wrong = request(checks, "/usr/bin/id", "-u", reason="e2e: wrong approver")
    approve(checks, wrong, WRONG_TELEGRAM_USER_ID, "wrong telegram user")
    rejected = run_helper(wrong)
    checks.check(
        "approval by another telegram user refused",
        rejected.returncode != 0 and "approver_mismatch" in rejected.stderr,
        short(rejected),
    )

    print("\n== f. a correct approval runs exactly once ==")
    approved_id = request(checks, "/usr/bin/id", "-u", reason="e2e: approved argv")
    approve(checks, approved_id, TELEGRAM_USER_ID, "configured telegram user")
    executed = run_helper(approved_id)
    checks.check(
        "approved argv runs as root (id -u prints 0)",
        executed.returncode == 0 and executed.stdout.strip() == "0",
        short(executed),
    )
    replay = run_helper(approved_id)
    checks.check(
        "second run of the same approval refused",
        replay.returncode != 0 and "replay" in replay.stderr,
        short(replay),
    )

    print("\n== argv binding ==")
    bound = request(checks, "/usr/bin/id", "-u", reason="e2e: tamper the request")
    approve(checks, bound, TELEGRAM_USER_ID, "then tamper")
    request_path = AGENT_HOME / ".hermes" / "consent" / f"{bound}.json"
    tampered = json.loads(request_path.read_text(encoding="utf-8"))
    tampered["argv"] = ["/usr/bin/id"]
    request_path.write_text(json.dumps(tampered), encoding="utf-8")
    tamper_run = run_helper(bound)
    checks.check(
        "edited request argv refused",
        tamper_run.returncode != 0 and "request_tampered" in tamper_run.stderr,
        short(tamper_run),
    )

    swapped = request(checks, "/usr/bin/id", "-u", reason="e2e: swap the approval")
    approve(checks, swapped, TELEGRAM_USER_ID, "then swap the hash")
    approval_path = AGENT_HOME / ".hermes" / "consent" / f"{swapped}.approved"
    record = json.loads(approval_path.read_text(encoding="utf-8"))
    record["argv_sha256"] = "0" * 64
    approval_path.write_text(json.dumps(record), encoding="utf-8")
    swap_run = run_helper(swapped)
    checks.check(
        "approval covering other argv refused",
        swap_run.returncode != 0 and "argv_hash_mismatch" in swap_run.stderr,
        short(swap_run),
    )

    print("\n== e. expired approvals are refused ==")
    install(checks, "--ttl-minutes", "0")
    stale = request(checks, "/usr/bin/id", "-u", reason="e2e: ttl 0")
    approve(checks, stale, TELEGRAM_USER_ID, "already expired")
    expired = run_helper(stale)
    checks.check(
        "expired approval refused",
        expired.returncode != 0 and "expired" in expired.stderr,
        short(expired),
    )

    return checks.summary()


def audit_phase() -> int:
    checks = Checks()
    if os.geteuid() != 0:
        print("audit phase must run as root", file=sys.stderr)
        return 2
    if not LOG.is_file():
        checks.bad("audit log exists", str(LOG))
        return checks.summary()

    info = LOG.stat()
    checks.check(
        "audit log is root-only 0600",
        info.st_mode & 0o777 == 0o600 and info.st_uid == 0,
        oct(info.st_mode & 0o777),
    )

    print("\n== sudoers rule is exactly one narrow command ==")
    sudoers_info = SUDOERS.stat()
    checks.check(
        "sudoers fragment is root:root 0440",
        sudoers_info.st_mode & 0o777 == 0o440 and sudoers_info.st_uid == 0,
        oct(sudoers_info.st_mode & 0o777),
    )
    rules = [
        line.strip()
        for line in SUDOERS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    checks.check("exactly one sudoers rule", len(rules) == 1, repr(rules))
    granted = rules[0].split("NOPASSWD:", 1)[-1].strip() if rules else ""
    checks.check(
        "rule grants only the helper, with no argument wildcard",
        granted == str(HELPER) and "*" not in granted and "," not in granted,
        rules[0] if rules else "missing",
    )

    lines = [
        line for line in LOG.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    records: list[dict[str, Any]] = []
    parse_ok = True
    for line in lines:
        try:
            records.append(json.loads(line))
        except ValueError:
            parse_ok = False
    checks.check(
        "every audit line is JSON",
        parse_ok and bool(records),
        f"{len(records)} line(s)",
    )

    allowed = [record for record in records if record.get("decision") == "allowed"]
    denied = [record for record in records if record.get("decision") == "denied"]
    checks.check(
        "one allowed execution logged", len(allowed) == 1, f"{len(allowed)} allowed"
    )
    if allowed:
        checks.check(
            "allowed record names the argv and the approver",
            allowed[0].get("argv") == ["/usr/bin/id", "-u"]
            and allowed[0].get("approver_telegram_user_id") == TELEGRAM_USER_ID,
            json.dumps(
                {k: allowed[0].get(k) for k in ("argv", "approver_telegram_user_id")}
            ),
        )

    reasons = {str(record.get("reason")) for record in denied}
    expected = {
        "no_request",
        "bad_id",
        "no_approval",
        "approver_mismatch",
        "replay",
        "expired",
    }
    checks.check(
        "denials logged for every refused case",
        expected.issubset(reasons),
        f"got {sorted(reasons)}",
    )
    checks.check(
        "allowed and denied both present",
        bool(allowed) and bool(denied),
        f"{len(allowed)} allowed / {len(denied)} denied",
    )

    print("\n== uninstall ==")
    removed = run([sys.executable, str(INSTALLER), "--uninstall"])
    checks.check(
        "uninstall removes the sudoers rule and helper",
        removed.returncode == 0 and not SUDOERS.exists() and not HELPER.exists(),
        short(removed),
    )

    def as_agent(*command: str) -> subprocess.CompletedProcess[str]:
        return run(
            ["runuser", "-u", AGENT_USER, "--", "env", f"HOME={AGENT_HOME}", *command]
        )

    still_narrow = as_agent("sudo", "-n", "true")
    checks.check(
        "sudo -n true still fails after uninstall",
        still_narrow.returncode != 0,
        short(still_narrow),
    )
    unreachable = as_agent(
        "sudo", "-n", str(HELPER), "run", "deadbeefdeadbeefdeadbeefdeadbeef"
    )
    checks.check(
        "helper is no longer reachable through sudo",
        unreachable.returncode != 0,
        short(unreachable),
    )
    return checks.summary()


def reset_state() -> None:
    """Start from a clean slate: bosn reuses its managed container."""
    run([sys.executable, str(INSTALLER), "--uninstall"])
    LOG.unlink(missing_ok=True)
    shutil.rmtree(STATE_DIR, ignore_errors=True)
    shutil.rmtree(AGENT_HOME / ".hermes" / "consent", ignore_errors=True)
    print("== reset: uninstalled any previous run, cleared log and state ==")


def both_phases() -> int:
    if os.geteuid() != 0:
        print(
            "--phase both must run as root (it drops to the agent user)",
            file=sys.stderr,
        )
        return 2
    reset_state()
    launcher = ["runuser", "-u", AGENT_USER, "--"]
    if run(["sh", "-c", "command -v runuser"]).returncode != 0:
        launcher = ["su", "-", AGENT_USER, "-c"]
    if launcher[0] == "runuser":
        command = [
            *launcher,
            "env",
            f"HOME={AGENT_HOME}",
            sys.executable,
            str(Path(__file__).resolve()),
            "--phase",
            "agent",
        ]
    else:
        script = str(Path(__file__).resolve())
        inner = f"HOME={AGENT_HOME} {sys.executable} {script} --phase agent"
        command = [*launcher, inner]
    agent = subprocess.run(command, cwd=str(REPO), check=False)
    print("\n== g. audit log (as root) ==")
    audit = audit_phase()
    return 0 if agent.returncode == 0 and audit == 0 else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("agent", "audit", "both"), default="both")
    args = parser.parse_args(argv[1:])
    if args.phase == "agent":
        return agent_phase()
    if args.phase == "audit":
        return audit_phase()
    return both_phases()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
