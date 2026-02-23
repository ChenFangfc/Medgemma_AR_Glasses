#!/usr/bin/env bash
set -euo pipefail

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux is not installed."
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

start_one() {
  local session="$1"
  local cmd="$2"
  if tmux has-session -t "$session" 2>/dev/null; then
    echo "$session already running"
    return 0
  fi
  tmux new-session -d -s "$session" "cd $ROOT_DIR && $cmd"
  echo "started tmux session: $session"
}

start_one medasr_ws \
  "while true; do MEDASR_GPU=0 $ROOT_DIR/miniforge3/envs/medasr/bin/uvicorn server_asr:app --host 0.0.0.0 --port 8001 --workers 1 --ws-max-size 33554432; echo '[medasr_ws] crashed, restarting in 2s'; sleep 2; done"

start_one medgemma_ws \
  "while true; do MEDGEMMA_GPU=1 $ROOT_DIR/miniforge3/envs/medgemma/bin/uvicorn server_gemma:app --host 0.0.0.0 --port 8002 --workers 1 --ws-max-size 33554432; echo '[medgemma_ws] crashed, restarting in 2s'; sleep 2; done"

start_one medworkflow_ws \
  "while true; do ASR_WS_URL=ws://127.0.0.1:8001/ws GEMMA_WS_URL=ws://127.0.0.1:8002/ws WORKFLOW_CONCURRENCY=1 $ROOT_DIR/miniforge3/envs/medasr/bin/uvicorn server_pipeline:app --host 0.0.0.0 --port 8003 --workers 1 --ws-max-size 67108864; echo '[medworkflow_ws] crashed, restarting in 2s'; sleep 2; done"
