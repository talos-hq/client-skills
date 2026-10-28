#!/usr/bin/env -S uv run --script
"""Validate every skill in skills/ before it can ship to a client.

Checks the frontmatter the agent actually matches on (name, description,
version), the directory/name agreement, and a few failure modes that have
bitten us: secrets committed into a skill, a description that reads like a
title instead of a trigger, and a skill with no verification section.

    uv run --no-project scripts/validate_skills.py
    uv run --no-project scripts/validate_skills.py --strict
    uv run --no-project scripts/validate_skills.py --include-templates

Exit code is 1 when any error is found, or when --strict is set and a warning
is found, so it can be wired straight into CI.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "skills"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
DESCRIPTION_MAX = 60
REQUIRED_KEYS = ("name", "description", "version", "author", "license")
SECRET_PATTERNS = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key block"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "GitHub token"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "API key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}"), "Telegram bot token"),
)


@dataclass
class Report:
    skill: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a SKILL.md into its top-level frontmatter keys and its body.

    Deliberately a hand parser: these files use a flat subset (scalars and
    inline lists) and the gate must run with no dependencies at all.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    keys: dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return keys, "\n".join(lines[index + 1 :])
        if not line or line.startswith((" ", "\t", "#")) or ":" not in line:
            continue
        key, _, value = line.partition(":")
        keys[key.strip()] = value.strip().strip("\"'")
    return {}, text


def check_frontmatter(
    skill_dir: Path, keys: dict[str, str], body: str, report: Report
) -> None:
    if not keys:
        report.errors.append("no frontmatter block (the file must start with '---')")
        return
    for key in REQUIRED_KEYS:
        if not keys.get(key):
            report.errors.append(f"frontmatter is missing `{key}`")
    name = keys.get("name", "")
    is_template = skill_dir.name.startswith("_")
    if name and name != skill_dir.name and not is_template:
        report.errors.append(
            f"frontmatter name '{name}' does not match directory '{skill_dir.name}'"
        )
    if name and not NAME_RE.match(name):
        report.errors.append(
            f"name '{name}' must be lowercase letters, digits and hyphens"
        )
    description = keys.get("description", "")
    if description:
        if len(description) > DESCRIPTION_MAX:
            report.errors.append(
                f"description is {len(description)} chars; the limit is {DESCRIPTION_MAX}"
            )
        if not description.lower().startswith("use when"):
            report.warnings.append(
                "description should read 'Use when <trigger>. <behaviour>.'"
            )
    version = keys.get("version", "")
    if version and not re.match(r"^\d+\.\d+\.\d+", version):
        report.errors.append(f"version '{version}' is not semver")
    if not body.strip():
        report.errors.append("SKILL.md has no body")
    lowered = body.lower()
    if "## verification" not in lowered:
        report.warnings.append(
            "no '## Verification' section — state the state, not the hope"
        )
    if "## changelog" not in lowered and not keys.get("deprecated"):
        report.warnings.append("no '## Changelog' section")


def check_secrets(text: str, report: Report) -> None:
    for pattern, label in SECRET_PATTERNS:
        match = pattern.search(text)
        if match:
            report.errors.append(
                f"looks like a committed {label}: {match.group(0)[:12]}…"
            )


def validate(skill_dir: Path) -> Report:
    report = Report(skill=skill_dir.name)
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        report.errors.append("missing SKILL.md")
        return report
    text = skill_md.read_text(encoding="utf-8")
    keys, body = parse_frontmatter(text)
    check_frontmatter(skill_dir, keys, body, report)
    check_secrets(text, report)
    return report


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict", action="store_true", help="treat warnings as errors"
    )
    parser.add_argument(
        "--include-templates",
        action="store_true",
        help="also check directories starting with '_' (e.g. _template)",
    )
    args = parser.parse_args(argv[1:])

    if not SKILLS.is_dir():
        print(f"::error::no skills/ directory at {SKILLS}")
        return 1

    directories = sorted(p for p in SKILLS.iterdir() if p.is_dir())
    if not args.include_templates:
        directories = [p for p in directories if not p.name.startswith("_")]
    if not directories:
        print("no skills to validate (add one under skills/<name>/SKILL.md)")
        return 0

    reports = [validate(path) for path in directories]
    failed = 0
    for report in reports:
        if report.errors or report.warnings:
            print(f"\n{report.skill}")
        for error in report.errors:
            print(f"  error   {error}")
        for warning in report.warnings:
            print(f"  warning {warning}")
        if report.errors or (args.strict and report.warnings):
            failed += 1

    errors = sum(len(report.errors) for report in reports)
    warnings = sum(len(report.warnings) for report in reports)
    print(f"\n{len(reports)} skill(s): {errors} error(s), {warnings} warning(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
