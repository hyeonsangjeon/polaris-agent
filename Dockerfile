# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.8.17 AS uv

FROM python:3.12-slim AS builder
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md LICENSE THIRD_PARTY_NOTICES.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim AS runtime
ARG POLARIS_UID=10001
ARG POLARIS_GID=10001
ENV PATH="/app/.venv/bin:${PATH}" \
    POLARIS_HOME=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN groupadd --gid "${POLARIS_GID}" polaris \
    && useradd --uid "${POLARIS_UID}" --gid "${POLARIS_GID}" \
        --home-dir /data --no-create-home --shell /usr/sbin/nologin polaris \
    && mkdir -p /app /data /workspace /exports \
    && chown -R polaris:polaris /app /data /workspace /exports
COPY --from=builder --chown=polaris:polaris /app/.venv /app/.venv
USER polaris:polaris
WORKDIR /workspace
VOLUME ["/data", "/workspace", "/exports"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=3).read()"]
ENTRYPOINT ["/app/.venv/bin/polarisd"]
CMD ["--config", "/data/config.json", "--host", "0.0.0.0", "--port", "8765", "--allow-remote"]
