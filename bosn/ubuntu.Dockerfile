# bosn stack: Ubuntu LTS. The repo is mounted read-only at /repo by bosn.toml,
# so nothing is COPYed here and every runtime write the suite makes lands in
# /tmp, /home/agent, /etc, /usr/local or /var.
FROM ubuntu:24.04@sha256:24737cef17f4e22b1977df7d9a05e77c4e28b7da5b8911f8d8ccda136bf3573c

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    UV_PYTHON=/usr/bin/python3 \
    UV_CACHE_DIR=/root/.cache/uv

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 sudo ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Pinned uv: the suite is launched with `uv run --no-project`, never bare python3.
COPY --from=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff /uv /uvx /usr/local/bin/

# A real unprivileged account with a password, so sudo genuinely prompts.
# This password is a throwaway fixture, not a credential.
RUN useradd --create-home --shell /bin/bash agent \
    && printf 'agent:consent-e2e-fixture\n' | chpasswd

# Harness only (not part of the thing under test): the unprivileged test user
# must be able to run *the installer* so the suite can prove the before/after
# state without an operator at a console. It grants exactly this one script;
# `sudo -n true`, /bin/sh and every other command still demand a password —
# which the suite asserts before and after installing.
RUN printf '%s\n' \
      'agent ALL=(root) NOPASSWD: /usr/bin/python3 /repo/scripts/install_consent_sudo.py *' \
      > /etc/sudoers.d/00-e2e-installer \
    && chmod 0440 /etc/sudoers.d/00-e2e-installer \
    && visudo -cf /etc/sudoers.d/00-e2e-installer

WORKDIR /tmp
