# Build from the repository root:
#   docker build -f examples/docker/Dockerfile -t ltc:0.7.0a1 .
# BASE_IMAGE overrides must provide Python 3.9+ and Debian/Ubuntu apt tooling.
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

RUN apt-get update \
    && apt-get install -y --no-install-recommends screen ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/ltc-source
COPY pyproject.toml ./
COPY src/ ./src/
RUN python -m pip install --no-cache-dir . \
    && rm -rf ./src ./build ./pyproject.toml

RUN useradd --create-home --uid 1000 --shell /bin/bash ltc \
    && mkdir -p /state/codex /state/claude /workspace \
    && chown -R ltc:ltc /state /workspace

ENV PYTHONUNBUFFERED=1 \
    CODEX_HOME=/state/codex \
    CLAUDE_CONFIG_DIR=/state/claude \
    LTC_EXECUTION_BACKEND=screen \
    CODEX_LONG_TASK_WAKEUP_DESKTOP_APP_SERVER=0

# Install the selected agent CLI and workload dependencies in a derived image.
# Agent sessions and authentication belong in runtime mounts, never image layers.
USER ltc
WORKDIR /workspace

# Run with docker --init. A detached setup command must not be the container CMD.
CMD ["ltc", "daemon"]
