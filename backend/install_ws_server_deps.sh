#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing websocket server dependencies in medgemma env..."
"$ROOT_DIR/miniforge3/envs/medgemma/bin/pip" install fastapi "uvicorn[standard]" websockets

echo "Installing websocket server dependencies in medasr env..."
"$ROOT_DIR/miniforge3/envs/medasr/bin/pip" install fastapi "uvicorn[standard]" websockets

echo "Done."
