# Agent image: Playwright's base ships Chromium + its system deps for amd64 and arm64.
# Keep the tag in lockstep with the playwright version in pyproject.toml.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY pyproject.toml ./
COPY agent ./agent
COPY migrations ./migrations
RUN pip install . && useradd --create-home --uid 10001 agent && mkdir -p /data && chown agent /data

USER agent
ENV DATA_DIR=/data
ENTRYPOINT ["ragent"]
