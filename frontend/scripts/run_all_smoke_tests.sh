#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

WS_URL="${WS_URL:-}"
PATIENT_ID="p_smoke_001"
TIMEOUT="45"
INSECURE=0
END_SESSION=0
VERBOSE=0
SAVE_JSON=""

SERVICE_MODE="user"

PACKAGE="com.DefaultCompany.Medgemma_glasses"
APK_PATH="test_v1.apk"
SERIAL=""
INSTALL_APK=1
AUTO_KEYS=1
LOG_SECONDS=12
LOG_FILE=""

SKIP_WORKFLOW=0
SKIP_SERVICES=0
SKIP_ADB=0

usage() {
  cat <<'EOF'
Usage: scripts/run_all_smoke_tests.sh [options]

Workflow options:
  --ws-url <url>          Workflow WS URL (or set WS_URL env)
  --patient-id <id>
  --timeout <seconds>
  --insecure
  --end-session
  --verbose
  --save-json <path>

Service check options:
  --user                  Check user services (default)
  --system                Check system services

ADB runtime options:
  --package <name>
  --apk <path>
  --serial <id>
  --no-install
  --no-auto-keys
  --log-seconds <n>
  --log-file <path>

Skip options:
  --skip-workflow
  --skip-services
  --skip-adb

  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ws-url)
      WS_URL="$2"
      shift 2
      ;;
    --patient-id)
      PATIENT_ID="$2"
      shift 2
      ;;
    --timeout)
      TIMEOUT="$2"
      shift 2
      ;;
    --insecure)
      INSECURE=1
      shift
      ;;
    --end-session)
      END_SESSION=1
      shift
      ;;
    --verbose)
      VERBOSE=1
      shift
      ;;
    --save-json)
      SAVE_JSON="$2"
      shift 2
      ;;
    --user)
      SERVICE_MODE="user"
      shift
      ;;
    --system)
      SERVICE_MODE="system"
      shift
      ;;
    --package)
      PACKAGE="$2"
      shift 2
      ;;
    --apk)
      APK_PATH="$2"
      shift 2
      ;;
    --serial)
      SERIAL="$2"
      shift 2
      ;;
    --no-install)
      INSTALL_APK=0
      shift
      ;;
    --no-auto-keys)
      AUTO_KEYS=0
      shift
      ;;
    --log-seconds)
      LOG_SECONDS="$2"
      shift 2
      ;;
    --log-file)
      LOG_FILE="$2"
      shift 2
      ;;
    --skip-workflow)
      SKIP_WORKFLOW=1
      shift
      ;;
    --skip-services)
      SKIP_SERVICES=1
      shift
      ;;
    --skip-adb)
      SKIP_ADB=1
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

overall_rc=0

if [[ $SKIP_WORKFLOW -eq 0 ]]; then
  echo "=== [1/3] Workflow WebSocket smoke test ==="
  workflow_cmd=(python3 "$SCRIPT_DIR/workflow_smoke_test.py" --patient-id "$PATIENT_ID" --timeout "$TIMEOUT")

  if [[ -n "$WS_URL" ]]; then
    workflow_cmd+=(--ws-url "$WS_URL")
  fi
  if [[ $INSECURE -eq 1 ]]; then
    workflow_cmd+=(--insecure)
  fi
  if [[ $END_SESSION -eq 1 ]]; then
    workflow_cmd+=(--end-session)
  fi
  if [[ $VERBOSE -eq 1 ]]; then
    workflow_cmd+=(--verbose)
  fi
  if [[ -n "$SAVE_JSON" ]]; then
    workflow_cmd+=(--save-json "$SAVE_JSON")
  fi

  if ! "${workflow_cmd[@]}"; then
    overall_rc=1
  fi
  echo
fi

if [[ $SKIP_SERVICES -eq 0 ]]; then
  echo "=== [2/3] WS service health check ==="
  service_cmd=(bash "$SCRIPT_DIR/check_ws_services.sh")
  if [[ "$SERVICE_MODE" == "system" ]]; then
    service_cmd+=(--system)
  else
    service_cmd+=(--user)
  fi
  if ! "${service_cmd[@]}"; then
    overall_rc=1
  fi
  echo
fi

if [[ $SKIP_ADB -eq 0 ]]; then
  echo "=== [3/3] ADB runtime smoke test ==="
  adb_cmd=(bash "$SCRIPT_DIR/adb_runtime_smoke.sh" --package "$PACKAGE" --apk "$APK_PATH" --log-seconds "$LOG_SECONDS")

  if [[ -n "$SERIAL" ]]; then
    adb_cmd+=(--serial "$SERIAL")
  fi
  if [[ $INSTALL_APK -eq 0 ]]; then
    adb_cmd+=(--no-install)
  fi
  if [[ $AUTO_KEYS -eq 1 ]]; then
    adb_cmd+=(--auto-keys)
  fi
  if [[ -n "$LOG_FILE" ]]; then
    adb_cmd+=(--log-file "$LOG_FILE")
  fi

  if ! "${adb_cmd[@]}"; then
    overall_rc=1
  fi
  echo
fi

if [[ $overall_rc -eq 0 ]]; then
  echo "PASS all selected smoke checks"
else
  echo "FAIL one or more smoke checks" >&2
fi

exit $overall_rc
