#!/usr/bin/env bash
set -euo pipefail

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux is not installed."
  exit 1
fi

stop_one() {
  local session="$1"
  if tmux has-session -t "$session" 2>/dev/null; then
    tmux kill-session -t "$session"
    echo "stopped tmux session: $session"
  else
    echo "$session is not running"
  fi
}

stop_one medasr_ws
stop_one medgemma_ws
stop_one medworkflow_ws
