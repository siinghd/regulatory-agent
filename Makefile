# Developer and operator shortcuts. Everything here is also spelled out in the scripts it calls.
SHELL := /bin/bash
.DEFAULT_GOAL := help

GIT_SHA := $(shell git rev-parse HEAD 2>/dev/null || echo unknown)$(shell test -z "$$(git status --porcelain --untracked-files=no 2>/dev/null)" || echo -dirty)
BUILD_ARGS := --build-arg GIT_SHA=$(GIT_SHA) --build-arg BUILD_DATE=$(shell date -u +%FT%TZ) \
              --build-arg OS_UPDATES_EPOCH=$(shell date -u +%G-W%V)
PG_IMAGE := postgres:16-alpine@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea
REDIS_IMAGE := redis:7-alpine@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499
UV_COMPILE := uv pip compile --universal --python-version 3.12 --generate-hashes --custom-compile-command "make lock"

.PHONY: help lock build test-image integration lint unit audit verify-roles validate-redis compliance deploy \
        metrics-env metrics-up metrics-down metrics-status metrics-check metrics-dashboards

help:  ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

lock:  ## regenerate the hashed lockfiles from pyproject.toml (keeps current pins where allowed)
	$(UV_COMPILE) pyproject.toml -o requirements.lock
	$(UV_COMPILE) pyproject.toml --extra dev -c requirements.lock -o requirements-dev.lock
	echo 'setuptools>=68' | $(UV_COMPILE) - -o requirements-build.lock

build:  ## build regulatory-agent:candidate and :candidate-test (never touches :latest)
	docker build $(BUILD_ARGS) --target release -t regulatory-agent:candidate .
	docker build $(BUILD_ARGS) --target test -t regulatory-agent:candidate-test .

test-image: build  ## unit + adversarial tests inside the candidate (no network, read-only, no caps)
	docker run --rm --network none --read-only --tmpfs /tmp:size=256m --cap-drop ALL \
	  --security-opt no-new-privileges:true -e HOME=/tmp regulatory-agent:candidate-test

integration:  ## integration tests in the candidate-test image against throwaway Postgres/Redis
	@docker rm -f ragent-it-pg ragent-it-redis >/dev/null 2>&1 || true
	docker run -d --name ragent-it-pg -e POSTGRES_USER=agent -e POSTGRES_PASSWORD=it-only -e POSTGRES_DB=agent \
	  -p 127.0.0.1:55499:5432 $(PG_IMAGE) >/dev/null
	docker run -d --name ragent-it-redis -p 127.0.0.1:56499:6379 $(REDIS_IMAGE) >/dev/null
	until docker exec ragent-it-pg pg_isready -U agent -q; do sleep 1; done; sleep 1
	docker run --rm --network host -e HOME=/tmp \
	  -e TEST_DATABASE_URL=postgresql://agent:it-only@127.0.0.1:55499/agent -e TEST_REDIS_URL=redis://127.0.0.1:56499/0 \
	  regulatory-agent:candidate-test -m integration -q -p no:cacheprovider; rc=$$?; \
	  docker rm -f ragent-it-pg ragent-it-redis >/dev/null; exit $$rc

lint:  ## ruff
	.venv/bin/ruff check agent tests deploy

unit:  ## unit + adversarial tests in the local venv
	.venv/bin/python -m pytest -q

audit:  ## known vulnerabilities in the locked dependencies
	for f in requirements.lock requirements-dev.lock requirements-build.lock; do \
	  uvx pip-audit==2.9.0 -r $$f --require-hashes --disable-pip --progress-spinner off || exit 1; done

verify-roles:  ## log in as each Postgres role and prove allowed/denied operations
	deploy/verify-db-roles.sh

validate-redis:  ## exercise arq/limits/breakers as the Redis agent user, probe forbidden commands
	REDIS_URL=$$(python3 deploy/lib/envtool.py get .env REDIS_URL) .venv/bin/python deploy/validate_redis_acl.py

compliance:  ## technical control checks (PASS/WARN/FAIL)
	deploy/compliance_check.sh

deploy:  ## build, test, scan, migrate, rolling restart, smoke, record (CHANGE=<PR ref> required)
	deploy/deploy.sh --change "$(CHANGE)"

# ---------------------------------------------------------------- observability (deploy/observability/README.md)
# These services also start with a plain `docker compose up -d`; the targets below touch only them.
OBS_SERVICES := prometheus alertmanager grafana node-exporter blackbox-exporter

metrics-env:  ## create deploy/observability/.env (mode 600) with a generated Grafana admin password
	@if [ -e deploy/observability/.env ]; then echo "deploy/observability/.env exists (kept)"; else \
	  umask 077; pw=$$(openssl rand -base64 48 | tr -d '/+=\n' | cut -c1-32); \
	  sed "s/^GF_SECURITY_ADMIN_PASSWORD=$$/GF_SECURITY_ADMIN_PASSWORD=$$pw/" deploy/observability/.env.example \
	    > deploy/observability/.env && chmod 600 deploy/observability/.env && \
	  echo "wrote deploy/observability/.env (Grafana admin user regagent-admin; password inside)"; fi

metrics-up: metrics-env  ## start the observability stack, wait until healthy, print the URLs
	docker compose up -d --wait --wait-timeout 180 $(OBS_SERVICES)
	docker compose run --rm --no-deps grafana-init
	@deploy/observability/status.sh

metrics-down:  ## stop and remove the observability containers (named volumes, i.e. history, are kept)
	docker compose stop $(OBS_SERVICES)
	docker compose rm -f $(OBS_SERVICES) grafana-init

metrics-status:  ## observability containers, Prometheus targets, probes, firing alerts, URLs
	@deploy/observability/status.sh

metrics-check:  ## promtool/amtool/blackbox checks, rule unit tests, dashboards match their generator
	deploy/observability/check.sh

metrics-dashboards:  ## regenerate deploy/observability/grafana/dashboards/*.json
	python3 deploy/observability/grafana/build_dashboards.py
