SHELL := /bin/zsh
UV_CACHE_DIR := $(CURDIR)/.cache/uv

.PHONY: setup backend-sync frontend-install api worker web lint typecheck test build check

setup: backend-sync frontend-install

backend-sync:
	cd backend && UV_CACHE_DIR=$(UV_CACHE_DIR) uv sync --all-groups

frontend-install:
	pnpm install

api:
	cd backend && UV_CACHE_DIR=$(UV_CACHE_DIR) uv run uvicorn robust_rag.main:app --reload --host 127.0.0.1 --port 8000

worker:
	cd backend && UV_CACHE_DIR=$(UV_CACHE_DIR) uv run celery -A robust_rag.workers.celery_app:celery_app worker --loglevel=INFO

web:
	pnpm --dir frontend dev

lint:
	cd backend && UV_CACHE_DIR=$(UV_CACHE_DIR) uv run ruff check .
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
