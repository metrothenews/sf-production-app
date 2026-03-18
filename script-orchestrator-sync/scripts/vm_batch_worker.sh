#!/usr/bin/env bash
set -euo pipefail

is_truthy() {
  local raw="${1:-}"
  raw="$(printf '%s' "${raw}" | tr '[:upper:]' '[:lower:]')"
  case "${raw}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

log() {
  printf '[vm-batch-worker] %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

if [[ -z "${CONTROL_SHEET_ID:-}" ]]; then
  log "ERROR: CONTROL_SHEET_ID is required."
  exit 1
fi

loop_interval="${BATCH_LOOP_INTERVAL_SECONDS:-20}"
if ! [[ "${loop_interval}" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  log "WARN: invalid BATCH_LOOP_INTERVAL_SECONDS='${loop_interval}', defaulting to 20"
  loop_interval="20"
fi

loop_once="${BATCH_LOOP_ONCE:-0}"
exit_on_error="${BATCH_LOOP_EXIT_ON_ERROR:-0}"

log "starting loop interval_seconds=${loop_interval} loop_once=${loop_once} exit_on_error=${exit_on_error}"

while true; do
  preflight_rc=0
  if ! python scripts/trigger_render_batch.py --preflight-only; then
    preflight_rc=$?
  fi

  if [[ "${preflight_rc}" -ne 0 ]]; then
    log "preflight_failed rc=${preflight_rc}"
    if is_truthy "${exit_on_error}"; then
      exit "${preflight_rc}"
    fi
  else
    log "preflight_ok"
  fi

  cycle_rc=0
  if ! python scripts/trigger_render_batch.py; then
    cycle_rc=$?
  fi

  case "${cycle_rc}" in
    0)
      log "batch_cycle_complete rc=0"
      ;;
    2)
      log "batch_cycle_busy rc=2"
      ;;
    *)
      log "batch_cycle_error rc=${cycle_rc}"
      if is_truthy "${exit_on_error}"; then
        exit "${cycle_rc}"
      fi
      ;;
  esac

  if is_truthy "${loop_once}"; then
    exit "${cycle_rc}"
  fi

  sleep "${loop_interval}"
done
