SHELL := /bin/zsh
UV_CACHE_DIR := $(CURDIR)/.cache/uv

.PHONY: setup backend-sync frontend-install migrate migration-check api worker worker-beat recover-jobs web lint typecheck test build check

setup: backend-sync frontend-install

backend-sync:
	cd backend && UV_CACHE_DIR=$(UV_CACHE_DIR) uv sync --all-groups

frontend-install:
	pnpm install

migrate:
	cd backend && UV_CACHE_DIR=$(UV_CACHE_DIR) uv run alembic upgrade head

migration-check:
	cd backend && UV_CACHE_DIR=$(UV_CACHE_DIR) uv run alembic check

api:
	cd backend && UV_CACHE_DIR=$(UV_CACHE_DIR) uv run uvicorn robust_rag.main:app --reload --host 127.0.0.1 --port 8000

worker:
	cd backend && UV_CACHE_DIR=$(UV_CACHE_DIR) uv run celery -A robust_rag.workers.celery_app:celery_app worker --loglevel=INFO

worker-beat:
	cd backend && UV_CACHE_DIR=$(UV_CACHE_DIR) uv run celery -A robust_rag.workers.celery_app:celery_app beat --loglevel=INFO

recover-jobs:
	cd backend && UV_CACHE_DIR=$(UV_CACHE_DIR) uv run celery -A robust_rag.workers.celery_app:celery_app call ingestion.recover_pending

web:
	pnpm --dir frontend dev

lint:
	cd backend && UV_CACHE_DIR=$(UV_CACHE_DIR) uv run ruff check .
	cd backend && UV_CACHE_DIR=$(UV_CACHE_DIR) uv run ruff format --check .
	pnpm --dir frontend lint

typecheck:
	cd backend && UV_CACHE_DIR=$(UV_CACHE_DIR) uv run mypy src tests
	pnpm --dir frontend typecheck

test:
	cd backend && UV_CACHE_DIR=$(UV_CACHE_DIR) uv run pytest
	pnpm --dir frontend test

build:
	pnpm --dir frontend build

check: lint typecheck test build
