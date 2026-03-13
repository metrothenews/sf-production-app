#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/sf-production-app}"
APP_DIR="${REPO_DIR}/script-orchestrator-sync"
PROJECT_REPO="${PROJECT_REPO:-https://github.com/metrothenews/sf-production-app.git}"
PROJECT_BRANCH="${PROJECT_BRANCH:-master}"
CURRENT_USER="${SUDO_USER:-${USER:-$(id -un)}}"
CURRENT_GROUP="$(id -gn "${CURRENT_USER}")"

if ! command -v apt-get >/dev/null 2>&1; then
  echo "ERROR: bootstrap_vm.sh supports Debian/Ubuntu hosts with apt-get."
  exit 1
fi

SUDO=""
if [[ "${EUID}" -ne 0 ]] && command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
fi

echo "[bootstrap] Installing base packages..."
${SUDO} apt-get update
${SUDO} apt-get install -y ca-certificates curl git make

if ! command -v docker >/dev/null 2>&1; then
  echo "[bootstrap] Installing Docker Engine + Compose plugin..."
  curl -fsSL https://get.docker.com | ${SUDO} sh
fi

${SUDO} systemctl enable --now docker

if id -nG "${CURRENT_USER}" | grep -qw docker; then
  echo "[bootstrap] User ${CURRENT_USER} already in docker group."
else
  echo "[bootstrap] Adding ${CURRENT_USER} to docker group..."
  ${SUDO} usermod -aG docker "${CURRENT_USER}"
  echo "[bootstrap] NOTE: re-login or run 'newgrp docker' before running docker without sudo."
fi

if [[ ! -d "${REPO_DIR}/.git" ]]; then
  echo "[bootstrap] Cloning repo into ${REPO_DIR}..."
  ${SUDO} mkdir -p "${REPO_DIR}"
  ${SUDO} chown -R "${CURRENT_USER}:${CURRENT_GROUP}" "${REPO_DIR}"
  git clone "${PROJECT_REPO}" "${REPO_DIR}"
fi

echo "[bootstrap] Updating repo branch ${PROJECT_BRANCH}..."
git -C "${REPO_DIR}" fetch origin "${PROJECT_BRANCH}"
git -C "${REPO_DIR}" checkout "${PROJECT_BRANCH}"
git -C "${REPO_DIR}" pull --ff-only origin "${PROJECT_BRANCH}"

mkdir -p "${APP_DIR}/secrets" "${APP_DIR}/work"
chmod 700 "${APP_DIR}/secrets"

if [[ ! -f "${APP_DIR}/.env.prod" ]]; then
  cp "${APP_DIR}/.env.prod.example" "${APP_DIR}/.env.prod"
  echo "[bootstrap] Created ${APP_DIR}/.env.prod from template."
fi

cat <<EOF
[bootstrap] Complete.
Next steps:
1) Edit ${APP_DIR}/.env.prod
2) For key-file auth, place JSON at ${APP_DIR}/secrets/google-application-credentials.json
3) Deploy:
   cd ${APP_DIR}
   make docker-build-prod
   make docker-up-prod
   curl -fsS http://127.0.0.1:\${APP_PORT:-8000}/healthz
EOF
