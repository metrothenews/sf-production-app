#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${CONTROL_SHEET_ID:-}" ]]; then
  echo "ERROR: CONTROL_SHEET_ID is required."
  exit 1
fi

if [[ -n "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]] && [[ ! -f "${GOOGLE_APPLICATION_CREDENTIALS}" ]]; then
  echo "ERROR: GOOGLE_APPLICATION_CREDENTIALS points to a missing file: ${GOOGLE_APPLICATION_CREDENTIALS}"
  exit 1
fi

require_bins="${REQUIRE_RENDER_BINARIES:-1}"
if [[ "${require_bins}" != "0" && "${require_bins,,}" != "false" && "${require_bins,,}" != "no" ]]; then
  blender_bin="${BLENDER_BIN:-blender}"
  ffmpeg_bin="${FFMPEG_BIN:-ffmpeg}"
  if ! command -v "${blender_bin}" >/dev/null 2>&1; then
    echo "ERROR: BLENDER_BIN is not executable: ${blender_bin}"
    exit 1
  fi
  if ! command -v "${ffmpeg_bin}" >/dev/null 2>&1; then
    echo "ERROR: FFMPEG_BIN is not executable: ${ffmpeg_bin}"
    exit 1
  fi
  if [[ ! -f "${VSE_STITCHER_SCRIPT_PATH:-}" ]]; then
    echo "ERROR: VSE_STITCHER_SCRIPT_PATH is missing: ${VSE_STITCHER_SCRIPT_PATH:-}"
    exit 1
  fi
  if [[ ! -f "${VSE_TEXT_SCRIPT_PATH:-}" ]]; then
    echo "ERROR: VSE_TEXT_SCRIPT_PATH is missing: ${VSE_TEXT_SCRIPT_PATH:-}"
    exit 1
  fi
fi

if [[ "$#" -gt 0 ]]; then
  exec "$@"
fi

exec uvicorn so_sync.api:app --host 0.0.0.0 --port 8000
