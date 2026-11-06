FROM debian:trixie-slim

ARG VOLTA_VERSION=2.0.2
ARG PYTHON_VERSION=3.10

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential ca-certificates curl fd-find ffmpeg git git-lfs gettext libffi-dev libpq-dev libssl-dev \
      pandoc pkg-config poppler-utils ripgrep xz-utils \
    && ln -s /usr/bin/fdfind /usr/local/bin/fd \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /usr/local/bin/

ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PYTHON_BIN_DIR=/usr/local/bin \
    UV_TOOL_DIR=/opt/uv-tools \
    UV_TOOL_BIN_DIR=/usr/local/bin \
    VOLTA_HOME=/opt/volta \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN uv python install --default --preview-features python-install-default "$PYTHON_VERSION" \
    && uv tool install prek \
    && uv tool install playwright \
    && playwright install --with-deps chromium \
    && ln -s "$(find /opt/ms-playwright -type f -name chrome -path '*chrome-linux*' | head -1)" /usr/local/bin/chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache

RUN curl -fsSL "https://github.com/volta-cli/volta/releases/download/v${VOLTA_VERSION}/volta-${VOLTA_VERSION}-linux.tar.gz" \
      | tar -xz -C /usr/local/bin \
    && volta install node@20 pnpm@10 yarn@1 \
    && for tool in node npm npx pnpm yarn; do ln -s /usr/local/bin/volta-shim "/usr/local/bin/$tool"; done \
    && chmod -R a+rwX /opt/volta

ENV HOME=/home/agent \
    UV_CACHE_DIR=/state/uv \
    UV_PYTHON_INSTALL_DIR=/state/python \
    npm_config_cache=/state/npm \
    npm_config_store_dir=/state/pnpm \
    YARN_CACHE_FOLDER=/state/yarn \
    PRE_COMMIT_HOME=/state/pre-commit \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8
RUN mkdir -p /home/agent /state && chmod 1777 /home/agent /state

WORKDIR /workspace
