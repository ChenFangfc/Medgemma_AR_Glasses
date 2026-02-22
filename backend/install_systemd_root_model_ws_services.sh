#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v sudo >/dev/null 2>&1; then
  echo "sudo is required for system-level service install."
  exit 1
fi

sudo tee /etc/systemd/system/medasr.service >/dev/null <<EOF
[Unit]
Description=MedASR WebSocket Service (GPU 0)
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$ROOT_DIR
Environment=MEDASR_GPU=0
ExecStart=$ROOT_DIR/miniforge3/envs/medasr/bin/uvicorn server_asr:app --host 0.0.0.0 --port 8001 --workers 1 --ws-max-size 33554432
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/medgemma.service >/dev/null <<EOF
[Unit]
Description=MedGemma WebSocket Service (GPU 1)
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$ROOT_DIR
Environment=MEDGEMMA_GPU=1
ExecStart=$ROOT_DIR/miniforge3/envs/medgemma/bin/uvicorn server_gemma:app --host 0.0.0.0 --port 8002 --workers 1 --ws-max-size 33554432
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/medworkflow.service >/dev/null <<EOF
[Unit]
Description=Med Workflow WebSocket Service (ASR -> Gemma)
After=network.target medasr.service medgemma.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$ROOT_DIR
Environment=ASR_WS_URL=ws://127.0.0.1:8001/ws
Environment=GEMMA_WS_URL=ws://127.0.0.1:8002/ws
Environment=WORKFLOW_CONCURRENCY=1
ExecStart=$ROOT_DIR/miniforge3/envs/medasr/bin/uvicorn server_pipeline:app --host 0.0.0.0 --port 8003 --workers 1 --ws-max-size 67108864
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now medasr.service medgemma.service medworkflow.service

echo "Installed system services:"
sudo systemctl --no-pager --full status medasr.service | sed -n '1,12p'
sudo systemctl --no-pager --full status medgemma.service | sed -n '1,12p'
sudo systemctl --no-pager --full status medworkflow.service | sed -n '1,12p'
