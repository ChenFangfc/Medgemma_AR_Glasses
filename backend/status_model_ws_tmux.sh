#!/usr/bin/env bash
set -euo pipefail

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux is not installed."
  exit 1
fi

status_one() {
  local session="$1"
  local health_url="$2"
  if tmux has-session -t "$session" 2>/dev/null; then
    echo "$session: running"
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
    echo "$session: stopped"
  fi
}

status_one medasr_ws "http://127.0.0.1:8001/health"
status_one medgemma_ws "http://127.0.0.1:8002/health"
status_one medworkflow_ws "http://127.0.0.1:8003/health"
