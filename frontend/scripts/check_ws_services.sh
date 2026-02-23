#!/usr/bin/env bash
set -euo pipefail

MODE="user"
FOLLOW=0

usage() {
  cat <<'EOF'
Usage: scripts/check_ws_services.sh [--user|--system] [--follow]

Options:
  --user    Check user services (default): medasr-ws.service, medgemma-ws.service, medworkflow-ws.service
  --system  Check system services: medasr.service, medgemma.service, medworkflow.service
  --follow  Follow workflow journal after status checks
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)
      MODE="user"
      shift
      ;;
    --system)
      MODE="system"
      shift
      ;;
    --follow)
      FOLLOW=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ "$MODE" == "user" ]]; then
  SERVICES=(medasr-ws.service medgemma-ws.service medworkflow-ws.service)
  STATUS_PREFIX=(systemctl --user status)
  JOURNAL_PREFIX=(journalctl --user -u)
  WORKFLOW_SERVICE="medworkflow-ws.service"
else
  SERVICES=(medasr.service medgemma.service medworkflow.service)
  STATUS_PREFIX=(sudo systemctl status)
  JOURNAL_PREFIX=(sudo journalctl -u)
  WORKFLOW_SERVICE="medworkflow.service"
fi

rc=0
echo "Checking services (mode=$MODE)..."

for svc in "${SERVICES[@]}"; do
  echo "===== $svc"
  if ! "${STATUS_PREFIX[@]}" "$svc" --no-pager; then
    rc=1
  fi
  echo

done

if [[ $FOLLOW -eq 1 ]]; then
  echo "Following logs for $WORKFLOW_SERVICE (Ctrl+C to stop)..."
  "${JOURNAL_PREFIX[@]}" "$WORKFLOW_SERVICE" -f
fi

exit $rc
