# Canonical development commands (see AGENTS.md section 3). Keep in sync with CI.
UV   ?= uv
PNPM ?= pnpm

.PHONY: help setup lint test test-int test-sec dev dev-backend dev-frontend migrate openapi \
        init-env up down logs build clean

help:
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | column -t -s "$$(printf '\t')"

setup: ## install backend and frontend dependencies
	cd backend && $(UV) sync
	cd frontend && $(PNPM) install --frozen-lockfile

lint: ## ruff, mypy, eslint, prettier --check, tsc
	cd backend && $(UV) run ruff check src tests && $(UV) run ruff format --check src tests && $(UV) run mypy src
	cd frontend && $(PNPM) exec tsc --noEmit && $(PNPM) exec eslint src && $(PNPM) exec prettier --check src

test: ## unit tests (no containers) + frontend tests
	cd backend && $(UV) run pytest tests/unit -q
	cd frontend && $(PNPM) exec vitest run

test-int: ## integration tests (testcontainers: PostgreSQL, MySQL, MongoDB; needs Docker)
	cd backend && $(UV) run pytest tests/integration -q

test-sec: ## security regression tests (needs Docker)
	cd backend && $(UV) run pytest -m security -q

dev-backend: ## run the API locally (needs DBW_* variables, see .env.example)
	cd backend && $(UV) run uvicorn app.main:app --reload --port 8000

dev-frontend: ## run the Vite dev server (proxies /api to :8000)
	cd frontend && $(PNPM) dev

migrate: ## run Alembic migrations against DBW_DATABASE_URL
	cd backend && $(UV) run alembic upgrade head

openapi: ## regenerate docs/openapi.json
	cd backend && $(UV) run python ../scripts/export_openapi.py > ../docs/openapi.json

init-env: ## generate .env with fresh secrets
	./scripts/init-env.sh

dev up: ## start the Docker Compose stack
	docker compose up -d --build

down: ## stop the stack (data volume is kept)
	docker compose down

logs: ## follow logs
	docker compose logs -f

build: ## build images
	docker compose build
