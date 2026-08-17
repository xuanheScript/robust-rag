#!/bin/zsh

set -eu

echo "Checking Redis..."
redis-cli -u "${REDIS_URL:-redis://127.0.0.1:6379/0}" ping

echo "Checking PostgreSQL..."
pg_isready -d "${DATABASE_URL:-postgresql://127.0.0.1:5432/postgres}"

echo "Checking cc switch..."
curl -fsS "${LLM_PROXY_HEALTH_URL:-http://127.0.0.1:15721/health}"
echo
