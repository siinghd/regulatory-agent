# syntax=docker/dockerfile:1
# Agent image. Playwright's base ships Chromium + its system deps for amd64 and arm64.
#
# Supply chain:
#   - the base is pinned by digest (multi-arch index); the tag must stay in lockstep with the
#     playwright pin in pyproject.toml. Dependabot proposes digest/tag bumps weekly.
#   - Ubuntu security updates the base tag has not picked up yet are applied in one layer,
#     refreshed weekly via OS_UPDATES_EPOCH (deploy/deploy.sh and CI pass the ISO week).
#   - every Python dependency is installed from requirements.lock with --require-hashes, and
#     sdists (dkimpy, pyspf) build against the hashed setuptools from requirements-build.lock
#     (--no-build-isolation: nothing unpinned is fetched at build time). Regenerate: `make lock`.
#   - the release image drops pip, virtualenv and caches: nothing installs packages at runtime
#     (the root filesystem is read-only anyway), and their vendored copies (urllib3, msgpack,
#     old setuptools wheels) stop showing up in scans.
#   - GIT_SHA becomes APP_VERSION (logs, deploy events) and the OCI revision label.
#
# Targets: `release` (default, what runs) and `test` (base + dev deps + tests; never deployed).
ARG BASE_IMAGE=mcr.microsoft.com/playwright/python:v1.63.0-noble@sha256:72bd171a9ffc2b4b59532aaa6210e21014d07093120dc25528870c0b840da1f0

FROM ${BASE_IMAGE} AS base

ARG OS_UPDATES_EPOCH=unset
RUN echo "OS security updates as of ${OS_UPDATES_EPOCH}" \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get -y --no-install-recommends upgrade \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# Dependencies first: this layer is rebuilt only when a lockfile changes.
COPY requirements-build.lock requirements.lock ./
RUN pip install --require-hashes --no-deps -r requirements-build.lock \
 && pip install --require-hashes --no-deps --no-build-isolation -r requirements.lock

COPY pyproject.toml ./
COPY agent ./agent
COPY migrations ./migrations
# Editable install: `ragent` must import /app/agent, because agent/db.py finds migrations/
# relative to the package. A regular install imports a copy in dist-packages that sees no
# migrations, and `ragent migrate` then silently applies nothing (the case before this file
# changed). The check below fails the build if that ever comes back.
RUN pip install --no-deps --no-build-isolation -e . \
 && pip check \
 && cd / && python -c "import sys, agent.db as d; n = len(list(d.MIGRATIONS.glob('*.sql'))); \
print(f'{n} migrations visible at {d.MIGRATIONS}'); sys.exit(0 if n else 'no migrations visible to the installed package')" \
 && useradd --create-home --uid 10001 agent && mkdir -p /data && chown agent /data

ARG GIT_SHA=unknown
ARG BUILD_DATE=unknown
ARG SOURCE_URL=
ARG BASE_IMAGE
LABEL org.opencontainers.image.title="regulatory-agent" \
      org.opencontainers.image.description="Email agent that fetches regulatory filings and replies with cited summaries" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.version="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.base.name="${BASE_IMAGE}"
ENV APP_VERSION=${GIT_SHA} DATA_DIR=/data
USER agent
ENTRYPOINT ["ragent"]


# Unit + adversarial tests on the same OS layer, Python and locked dependencies:
#   docker build --target test -t regulatory-agent:candidate-test . &&
#   docker run --rm --network none regulatory-agent:candidate-test
FROM base AS test
USER root
COPY requirements-dev.lock ./
RUN pip install --require-hashes --no-deps --no-build-isolation -r requirements-dev.lock && pip check
COPY tests ./tests
USER agent
ENTRYPOINT ["python", "-m", "pytest"]
CMD ["-q", "-p", "no:cacheprovider"]


FROM base AS release
USER root
RUN python -m pip uninstall -y -q virtualenv distlib filelock platformdirs python-discovery pip \
 && rm -rf /root/.cache /home/agent/.cache \
 && cd / && python -c "import agent.cli, agent.worker, agent.web.app, agent.mail.ingest"
USER agent
