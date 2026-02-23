#!/usr/bin/env bash
set -euo pipefail

PACKAGE="com.DefaultCompany.Medgemma_glasses"
APK_PATH="test_v1.apk"
SERIAL=""
INSTALL_APK=1
AUTO_KEYS=0
LOG_SECONDS=12
LOG_FILE=""
CLEAR_LOGCAT=1

usage() {
  cat <<'EOF'
Usage: scripts/adb_runtime_smoke.sh [options]

Options:
  --package <name>      Android package name (default: com.DefaultCompany.Medgemma_glasses)
  --apk <path>          APK path for install -r (default: test_v1.apk)
  --no-install          Skip APK install
  --serial <id>         adb device serial
  --auto-keys           Send basic key events (single/double Return, Menu, Left, Right)
  --log-seconds <n>     Log capture duration in seconds (default: 12)
  --log-file <path>     Output log file (default: logs/adb_runtime_smoke_<timestamp>.log)
  --no-clear-logcat     Do not clear logcat before capture
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --package)
      PACKAGE="$2"
      shift 2
      ;;
    --apk)
      APK_PATH="$2"
      shift 2
      ;;
    --no-install)
      INSTALL_APK=0
      shift
      ;;
    --serial)
      SERIAL="$2"
      shift 2
      ;;
    --auto-keys)
      AUTO_KEYS=1
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
    --no-clear-logcat)
      CLEAR_LOGCAT=0
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

if ! command -v adb >/dev/null 2>&1; then
  echo "adb not found in PATH" >&2
  exit 2
fi

adb_cmd() {
  if [[ -n "$SERIAL" ]]; then
    adb -s "$SERIAL" "$@"
  else
    adb "$@"
  fi
}

if [[ -z "$LOG_FILE" ]]; then
  mkdir -p logs
  LOG_FILE="logs/adb_runtime_smoke_$(date +%Y%m%d_%H%M%S).log"
fi

if [[ $INSTALL_APK -eq 1 ]]; then
  if [[ ! -f "$APK_PATH" ]]; then
    echo "APK not found: $APK_PATH" >&2
    exit 2
  fi
  echo "Installing APK: $APK_PATH"
  adb_cmd install -r "$APK_PATH"
fi

echo "Launching app: $PACKAGE"
adb_cmd shell monkey -p "$PACKAGE" 1 >/dev/null

PID=""
for _ in {1..20}; do
  PID="$(adb_cmd shell pidof "$PACKAGE" 2>/dev/null | tr -d '\r' | awk '{print $1}')"
  if [[ -n "$PID" ]]; then
    break
  fi
  sleep 0.5

done

if [[ -z "$PID" ]]; then
  echo "Failed to resolve app PID for package: $PACKAGE" >&2
  exit 1
fi

echo "Resolved PID: $PID"

if [[ $CLEAR_LOGCAT -eq 1 ]]; then
  adb_cmd logcat -c
fi

if [[ $AUTO_KEYS -eq 1 ]]; then
  echo "Sending key events..."
  adb_cmd shell input keyevent 66    # Return single
  sleep 0.5
  adb_cmd shell input keyevent 66    # Return double #1
  sleep 0.08
  adb_cmd shell input keyevent 66    # Return double #2
  sleep 0.3
  adb_cmd shell input keyevent 82    # Menu
  sleep 0.3
  adb_cmd shell input keyevent 21    # Left
  sleep 0.1
  adb_cmd shell input keyevent 22    # Right
fi

echo "Capturing logcat for ${LOG_SECONDS}s -> $LOG_FILE"
adb_cmd logcat --pid="$PID" -v time > "$LOG_FILE" &
LOGCAT_PID=$!

cleanup() {
  if kill -0 "$LOGCAT_PID" >/dev/null 2>&1; then
    kill "$LOGCAT_PID" >/dev/null 2>&1 || true
    wait "$LOGCAT_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

sleep "$LOG_SECONDS"
cleanup
trap - EXIT

echo "Filtered highlights:"
if ! grep -E "ArWheelInputRouter|ArGlassesClinicalAssistant|WebSocket|Processing|Ready|Offline|Online|NextViewPage|GoHomeView" "$LOG_FILE"; then
  echo "(no highlight matches found; inspect full log file)"
fi

echo "Done. Full log: $LOG_FILE"
