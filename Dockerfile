# syntax=docker/dockerfile:1

# Pinned by tag; pin by digest as well for production promotion.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# ---- dependencies ---------------------------------------------------------
FROM ${PYTHON_IMAGE} AS deps
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
RUN python -m venv /opt/venv
COPY requirements/runtime.lock /tmp/runtime.lock
RUN /opt/venv/bin/pip install --require-hashes --only-binary=:all: -r /tmp/runtime.lock

# ---- runtime --------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime
ARG APP_UID=10001
ARG APP_GID=10001
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system --gid "${APP_GID}" iris \
 && useradd --system --uid "${APP_UID}" --gid iris --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin iris \
 && mkdir -p /data/output /data/source \
 && chown iris:iris /data/output

COPY --from=deps /opt/venv /opt/venv
COPY src /app/src

# Configuration and secrets are supplied at runtime only; nothing
# deployment-specific is baked into the image.
WORKDIR /app
USER iris:iris
ENTRYPOINT ["python", "-m", "iris_pilot"]
CMD ["run"]

# ---- test -----------------------------------------------------------------
FROM runtime AS test
USER root
COPY requirements/test.lock /tmp/test.lock
RUN /opt/venv/bin/pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/test.lock \
 && rm /tmp/test.lock
COPY pyproject.toml docker-compose.yml Dockerfile .dockerignore /app/
COPY deploy /app/deploy
COPY fixtures /app/fixtures
COPY tests /app/tests
USER iris:iris
ENTRYPOINT ["python", "-m", "pytest", "-p", "no:cacheprovider"]
CMD []

# Default target for a plain `docker build .` is the runtime image.
FROM runtime
