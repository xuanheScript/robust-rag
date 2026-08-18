#!/bin/zsh

set -eu

echo "Checking Redis..."
redis-cli -u "${REDIS_URL:-redis://127.0.0.1:6379/0}" ping

echo "Checking PostgreSQL..."
pg_isready -d "${DATABASE_URL:-postgresql://127.0.0.1:5432/postgres}"

echo "Checking direct LLM API configuration..."
if [[ -z "${LLM_BASE_URL:-}" || -z "${LLM_API_KEY:-}" || -z "${LLM_MODEL:-}" ]]; then
  echo "LLM_BASE_URL, LLM_API_KEY and LLM_MODEL are required" >&2
  exit 1
fi
echo "LLM API configuration present. Remote contract calls are intentionally opt-in."
