# syntax=docker/dockerfile:1.7
ARG PYTHON_IMAGE=python:3.13.14-slim-bookworm@sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64

FROM ${PYTHON_IMAGE} AS builder

ARG POETRY_VERSION=2.4.1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=true
WORKDIR /app

RUN python -m pip install "poetry==${POETRY_VERSION}"
COPY pyproject.toml poetry.lock README.md ./
RUN poetry sync --only main --no-root --no-ansi

COPY src ./src
RUN rm ./src/ig_trader/db_bootstrap.py
RUN poetry install --only-root --no-ansi

FROM ${PYTHON_IMAGE} AS runtime

ARG APP_COMMIT_SHA=unknown
ARG APP_SOURCE_URL=local
ARG APP_VERSION=0.1.0
LABEL org.opencontainers.image.title="IG Trader safe cloud runtime" \
      org.opencontainers.image.description="NO_EXECUTION health boundary for IG Trader" \
      org.opencontainers.image.revision="${APP_COMMIT_SHA}" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.source="${APP_SOURCE_URL}"

ENV APP_COMMIT_SHA="${APP_COMMIT_SHA}" \
    APP_HOST=0.0.0.0 \
    APP_PORT=8080 \
    APP_VERSION="${APP_VERSION}" \
    EXECUTION_MODE=NO_EXECUTION \
    LOG_LEVEL=INFO \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SHUTDOWN_GRACE_SECONDS=10

RUN groupadd --gid 10001 app && \
    useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app && \
    mkdir -p /app && \
    chown 10001:10001 /app
WORKDIR /app
COPY --from=builder --chown=10001:10001 /app/.venv ./.venv
COPY --from=builder --chown=10001:10001 /app/src ./src

USER 10001:10001
EXPOSE 8080
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/live', timeout=2).read()"]
ENTRYPOINT ["python", "-m", "src.ig_trader.cloud_runtime"]
