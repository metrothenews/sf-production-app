#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BRANCH="${DEPLOY_BRANCH:-master}"
APP_PORT="${APP_PORT:-8000}"
COMPOSE_FILES=(-f docker-compose.prod.yml)

cd "${REPO_DIR}"

if [[ ! -f ".env.prod" ]]; then
  echo "ERROR: Missing .env.prod in ${REPO_DIR}. Copy .env.prod.example and fill required values."
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker daemon is not available."
  exit 1
fi

echo "[deploy] Updating repo (${BRANCH})..."
git fetch origin "${BRANCH}"
git checkout "${BRANCH}"
git pull --ff-only origin "${BRANCH}"

echo "[deploy] Building production image..."
docker compose "${COMPOSE_FILES[@]}" build app

echo "[deploy] Restarting app container..."
docker compose "${COMPOSE_FILES[@]}" up -d app

echo "[deploy] Waiting for health endpoint..."
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${APP_PORT}/healthz" >/dev/null; then
    echo "[deploy] Healthy on port ${APP_PORT}."
    exit 0
  fi
  sleep 2
done

echo "[deploy] ERROR: health check did not pass in time."
docker compose "${COMPOSE_FILES[@]}" ps
exit 1
