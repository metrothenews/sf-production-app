#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BRANCH="${DEPLOY_BRANCH:-master}"
APP_PORT="${APP_PORT:-8000}"
DEPLOY_WITH_API="${DEPLOY_WITH_API:-1}"
COMPOSE_FILES=(-f docker-compose.prod.yml -f docker-compose.vm-batch.yml)

is_truthy() {
  local raw="${1:-}"
  raw="$(printf '%s' "${raw}" | tr '[:upper:]' '[:lower:]')"
  case "${raw}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

cd "${REPO_DIR}"

if [[ ! -f ".env.prod" ]]; then
  echo "ERROR: Missing .env.prod in ${REPO_DIR}. Copy .env.prod.example and fill required values."
  exit 1
fi

if [[ ! -f ".env.vm" ]]; then
  echo "ERROR: Missing .env.vm in ${REPO_DIR}. Copy .env.vm.example and set VM overrides."
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker daemon is not available."
  exit 1
fi

echo "[deploy-batch] Updating repo (${BRANCH})..."
git fetch origin "${BRANCH}"
git checkout "${BRANCH}"
git pull --ff-only origin "${BRANCH}"

echo "[deploy-batch] Building batch-worker image..."
docker compose "${COMPOSE_FILES[@]}" build batch-worker

if is_truthy "${DEPLOY_WITH_API}"; then
  echo "[deploy-batch] Restarting app + batch-worker containers..."
  docker compose "${COMPOSE_FILES[@]}" up -d app batch-worker

  echo "[deploy-batch] Waiting for API health endpoint..."
  for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${APP_PORT}/healthz" >/dev/null; then
      echo "[deploy-batch] API healthy on port ${APP_PORT}."
      break
    fi
    sleep 2
  done
else
  echo "[deploy-batch] Restarting batch-worker container only..."
  docker compose "${COMPOSE_FILES[@]}" up -d batch-worker
fi

if ! docker compose "${COMPOSE_FILES[@]}" ps batch-worker | grep -q "batch-worker"; then
  echo "[deploy-batch] ERROR: batch-worker container is not running."
  docker compose "${COMPOSE_FILES[@]}" ps
  exit 1
fi

echo "[deploy-batch] batch-worker is running."
docker compose "${COMPOSE_FILES[@]}" ps
