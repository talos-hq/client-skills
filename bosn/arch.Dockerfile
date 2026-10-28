# bosn stack: Arch Linux. Deliberately a different package manager and layout
# from Ubuntu — pacman, no apt, visudo from the sudo package — to prove the
# installer makes no Debian assumptions. Repo is mounted read-only at /repo.
FROM archlinux:latest@sha256:e82623936fa1a69700b48e98b058df6eaab18b737c27473a7234a40fdecb9413

ENV PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    UV_PYTHON=/usr/bin/python3 \
    UV_CACHE_DIR=/root/.cache/uv

RUN pacman -Syu --noconfirm --needed \
        sudo python ca-certificates curl shadow \
    && pacman -Scc --noconfirm

COPY --from=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff /uv /uvx /usr/local/bin/

# Same unprivileged fixture account: non-root, has a password, so sudo prompts.
RUN useradd --create-home --shell /bin/bash agent \
    && printf 'agent:consent-e2e-fixture\n' | chpasswd

# Harness only: exactly one script, nothing else. See bosn/ubuntu.Dockerfile.
RUN printf '%s\n' \
      'agent ALL=(root) NOPASSWD: /usr/bin/python3 /repo/scripts/install_consent_sudo.py *' \
      > /etc/sudoers.d/00-e2e-installer \
    && chmod 0440 /etc/sudoers.d/00-e2e-installer \
    && visudo -cf /etc/sudoers.d/00-e2e-installer

WORKDIR /tmp
