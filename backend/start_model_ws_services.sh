#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$ROOT_DIR/.run"
LOG_DIR="$ROOT_DIR/.logs"

mkdir -p "$RUN_DIR" "$LOG_DIR"
cd "$ROOT_DIR"

start_one() {
  local name="$1"
  shift
  local pid_file="$RUN_DIR/${name}.pid"
  local log_file="$LOG_DIR/${name}.log"

  if [[ -f "$pid_file" ]]; then
    local pid
    pid="$(cat "$pid_file" || true)"
    if [[ -n "${pid}" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "$name already running (pid $pid)"
      return 0
    fi
    rm -f "$pid_file"
  fi

  nohup "$@" >>"$log_file" 2>&1 &
  local pid=$!
  echo "$pid" >"$pid_file"
  sleep 1

  if kill -0 "$pid" 2>/dev/null; then
    echo "started $name (pid $pid), log: $log_file"
  else
    echo "failed to start $name, check: $log_file"
    rm -f "$pid_file"
    return 1
  fi
}

start_one medasr_ws \
  env MEDASR_GPU=0 \
  "$ROOT_DIR/miniforge3/envs/medasr/bin/uvicorn" \
  server_asr:app \
  --host 0.0.0.0 --port 8001 --workers 1 --ws-max-size 33554432

start_one medgemma_ws \
  env MEDGEMMA_GPU=1 \
  "$ROOT_DIR/miniforge3/envs/medgemma/bin/uvicorn" \
  server_gemma:app \
  --host 0.0.0.0 --port 8002 --workers 1 --ws-max-size 33554432

start_one medworkflow_ws \
  env ASR_WS_URL=ws://127.0.0.1:8001/ws GEMMA_WS_URL=ws://127.0.0.1:8002/ws WORKFLOW_CONCURRENCY=1 \
  "$ROOT_DIR/miniforge3/envs/medasr/bin/uvicorn" \
  server_pipeline:app \
  --host 0.0.0.0 --port 8003 --workers 1 --ws-max-size 67108864
