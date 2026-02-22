#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_SYSTEMD_DIR="$HOME/.config/systemd/user"

if ! systemctl --user show-environment >/dev/null 2>&1; then
  echo "No usable systemd user bus in this shell/session."
  echo
  echo "Pick one of these:"
  echo "1) If you have sudo, run:  bash \"$ROOT_DIR/install_systemd_root_model_ws_services.sh\""
  echo "2) No sudo: use tmux fallback:"
  echo "   bash \"$ROOT_DIR/start_model_ws_tmux.sh\""
  echo "   bash \"$ROOT_DIR/status_model_ws_tmux.sh\""
  exit 1
fi

mkdir -p "$USER_SYSTEMD_DIR"
cp "$ROOT_DIR/systemd/medgemma-ws.service" "$USER_SYSTEMD_DIR/"
cp "$ROOT_DIR/systemd/medasr-ws.service" "$USER_SYSTEMD_DIR/"
cp "$ROOT_DIR/systemd/medworkflow-ws.service" "$USER_SYSTEMD_DIR/"

systemctl --user daemon-reload
systemctl --user enable --now medgemma-ws.service
systemctl --user enable --now medasr-ws.service
systemctl --user enable --now medworkflow-ws.service

echo "Installed and started user services:"
systemctl --user --no-pager --full status medgemma-ws.service | sed -n '1,12p'
systemctl --user --no-pager --full status medasr-ws.service | sed -n '1,12p'
systemctl --user --no-pager --full status medworkflow-ws.service | sed -n '1,12p'
