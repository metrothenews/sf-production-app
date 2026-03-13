#!/usr/bin/env bash
set -euo pipefail

key_json="${DRIVE_SA_KEY_JSON:-}"
key_file="${DRIVE_SA_KEY_FILE:-/secrets/drive-sa.json}"

if [[ -n "${key_json}" ]]; then
  mkdir -p "$(dirname "${key_file}")"
  printf '%s' "${key_json}" > "${key_file}"
  chmod 600 "${key_file}"
fi

if [[ -f "${key_file}" ]]; then
  export GOOGLE_APPLICATION_CREDENTIALS="${key_file}"
fi

if [[ "$#" -eq 0 ]]; then
  echo "ERROR: batch_entrypoint.sh requires a command to execute."
  exit 1
fi

exec "$@"
