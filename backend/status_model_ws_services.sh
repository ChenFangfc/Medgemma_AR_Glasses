#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$ROOT_DIR/.run"

status_one() {
  local name="$1"
  local health_url="$2"
  local pid_file="$RUN_DIR/${name}.pid"

  if [[ ! -f "$pid_file" ]]; then
    echo "$name: stopped (no pid file)"
    return 0
  fi

  local pid
  pid="$(cat "$pid_file" || true)"
  if [[ -z "$pid" ]]; then
    echo "$name: stopped (empty pid file)"
    return 0
  fi

  if kill -0 "$pid" 2>/dev/null; then
    echo "$name: running (pid $pid)"
    if command -v curl >/dev/null 2>&1; then
      local health
      health="$(curl -fsS "$health_url" || true)"
      if [[ -n "$health" ]]; then
        echo "  health: $health"
      else
        echo "  health: unavailable"
      fi
    fi
  else
    echo "$name: stale pid file (pid $pid not running)"
  fi
}

status_one medasr_ws "http://127.0.0.1:8001/health"
status_one medgemma_ws "http://127.0.0.1:8002/health"
status_one medworkflow_ws "http://127.0.0.1:8003/health"
