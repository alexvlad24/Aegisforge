# Run from WSL Ubuntu (Docker Desktop engine + gVisor-free hardened runc, see SPEC §5.3).
# uv keeps its Linux environment in .venv-linux; the Windows .venv is left alone.
export UV_PROJECT_ENVIRONMENT ?= .venv-linux
export PYTHONPATH := $(CURDIR)

COMPOSE := docker compose --project-directory . -f infra/docker-compose.yml --env-file .env
SANDBOX_IMAGE := aegis-sandbox:latest

.PHONY: check env up down build-sandbox seed lint typecheck test test-unit test-security test-integration clean-runs

# Acceptance gate for every week (SPEC Appendix B).
check: up seed lint typecheck test

env:
	uv run python scripts/make_env.py

up: env build-sandbox
	$(COMPOSE) up -d --wait
	@echo "postgres on 127.0.0.1:55432, phoenix on 127.0.0.1:6006"

down:
	$(COMPOSE) down

build-sandbox:
	docker build -t $(SANDBOX_IMAGE) infra/sandbox

seed:
	uv run python victim/seed.py

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy

test: test-unit test-security test-integration

test-unit:
	uv run pytest tests/unit -q

test-security:
	uv run pytest tests/security -q -m security

test-integration:
	uv run pytest tests/integration -q -m integration

clean-runs:
	rm -rf runs
