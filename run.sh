#!/usr/bin/env bash
# Launch the camera app: go2rtc (ingest/live view) + the web UI.
# Works on the Android phone (Termux) and on a Mac/Linux dev box.
# Recorder/motion/NAS-sync are started by `python -m camapp` (added in the next phase).
set -u
cd "$(dirname "$0")"

# Stop any leftover instance from a previous run before starting a new one.
# Two recorders writing the same buffer files corrupt segments and produce
# duplicate clips, so this app must run as a single instance.
# (bracket avoids pkill treating "-m" as an option, and matches case-independently.)
echo "Stopping any previous instance…"
pkill -f "[-]m camapp" 2>/dev/null || true
pkill -f "go2rtc -config go2rtc.yaml" 2>/dev/null || true
sleep 1

# Load .env into the environment (so go2rtc can expand ${RTSP_MAIN_URL}).
set -a
[ -f .env ] && . ./.env
set +a

# Pick a go2rtc binary: one in PATH (Termux: `pkg install go2rtc` or a downloaded
# go2rtc_linux_arm64), else the local tools/bin/go2rtc.
GO2RTC="$(command -v go2rtc || true)"
[ -z "$GO2RTC" ] && [ -x tools/bin/go2rtc ] && GO2RTC=tools/bin/go2rtc
if [ -z "$GO2RTC" ]; then
  echo "go2rtc not found. Install it (Termux: pkg install go2rtc) or put a binary at tools/bin/go2rtc." >&2
  exit 1
fi

PIDS=()
cleanup() { kill "${PIDS[@]}" 2>/dev/null; }
trap cleanup EXIT INT TERM

echo "Starting go2rtc…"
"$GO2RTC" -config go2rtc.yaml &
PIDS+=($!)

# Prefer the project virtualenv if present (Mac dev), else python3 (Termux).
if [ -z "${PYTHON:-}" ]; then
  if [ -x .venv/bin/python ]; then PYTHON=.venv/bin/python; else PYTHON=python3; fi
fi

echo "Starting web UI on port ${WEB_PORT:-8080}…"
"$PYTHON" -m camapp.web &
PIDS+=($!)

# Recorder + motion read go2rtc's restream. Set ENABLE_RECORDER=0 to run the live
# view alone (go2rtc + web) — useful for isolating whether recorder/motion churn is
# what destabilizes the live stream on a weak Wi-Fi link.
if [ "${ENABLE_RECORDER:-1}" = "1" ]; then
  echo "Starting recorder + motion detection…"
  "$PYTHON" -m camapp &
  PIDS+=($!)
else
  echo "Recorder + motion disabled (ENABLE_RECORDER=0); live view only."
fi

wait
