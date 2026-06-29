# Base: small, recent Python. Debian-slim keeps the image around 150 MB
# before we add LaTeX (which dominates the final size).
FROM python:3.12-slim

# --- System packages -------------------------------------------------------
# Split into one RUN so it's a single cacheable layer.
#   sudo/git/gnupg/openssh-client/socat/cron — dev + signing + scheduling
#   latexmk + texlive-*                      — full scientific LaTeX build
#   libpng/libfreetype/libfontconfig/zlib    — runtime deps for matplotlib,
#                                              python-pptx, pymupdf, etc.
RUN apt-get update && apt-get install -y \
    sudo git gnupg openssh-client socat cron \
    latexmk texlive-latex-extra texlive-fonts-recommended \
    texlive-science texlive-lang-german \
    libpng16-16 libfreetype6 libfontconfig1 zlib1g \
    && apt-get clean

# --- User + Claude/config plumbing ----------------------------------------
# uid/gid 1000 matches the typical host user, so bind-mounted files keep
# correct ownership without chown gymnastics.
#
# The two symlinks below are the key trick that makes this template
# reusable: Claude Code (and pptx_preview) look for config under the
# user's home, but we want the source-of-truth files to live in the
# bind-mounted /workspace so they travel with the project in git.
RUN groupadd -g 1000 vscode \
    && useradd -u 1000 -g 1000 -m -s /bin/bash vscode \
    && echo 'vscode ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers \
    # GPG home must be 0700 or gpg-agent refuses to start.
    && mkdir -p /home/vscode/.gnupg \
    && chmod 700 /home/vscode/.gnupg \
    && chown vscode:vscode /home/vscode/.gnupg \
    # ~/.claude → workspace/.claude: settings and plugins live with the project.
    && ln -sfn /workspace/.claude /home/vscode/.claude \
    && chown -h vscode:vscode /home/vscode/.claude \
    && mkdir -p /home/vscode/.config \
    && chown vscode:vscode /home/vscode/.config \
    # Same pattern for the pptx_preview tool's config.
    && ln -sfn /workspace/.config/pptx_preview /home/vscode/.config/pptx_preview \
    && chown -h vscode:vscode /home/vscode/.config/pptx_preview \
    # Persist bash history across container rebuilds by pointing HISTFILE
    # at the bind-mounted workspace. erasedups + histappend keeps it tidy.
    && printf '%s\n' \
        '' \
        '# Persist bash history across devcontainer rebuilds' \
        'export HISTFILE=/workspace/.devcontainer/bash_history' \
        'export HISTSIZE=10000' \
        'export HISTFILESIZE=20000' \
        'export HISTCONTROL=ignoredups:erasedups' \
        'shopt -s histappend' \
        'PROMPT_COMMAND="history -a; ${PROMPT_COMMAND:-}"' \
        >> /home/vscode/.bashrc

# --- Python tooling --------------------------------------------------------
# Kept in its own layer so changing this list doesn't bust the apt cache.
# Project-specific Python deps belong in the *workspace* (requirements.txt /
# pyproject.toml), not here — this set is just the agentic-workflow baseline.
RUN python3 -m pip install --no-cache-dir anthropic claude-monitor python-pptx pymupdf

# Drop privileges for the rest of the session. Anything that needs root
# can use `sudo` (passwordless, see /etc/sudoers above).
USER vscode

# Add ~/.local/bin so `pip install --user` binaries (and the `claude` CLI
# installed by postCreateCommand) are on PATH without further setup.
ENV PATH="/home/vscode/.local/bin:$PATH"
# Cache Playwright browsers in the workspace so they survive rebuilds.
ENV PLAYWRIGHT_BROWSERS_PATH=/workspace/.cache/ms-playwright
