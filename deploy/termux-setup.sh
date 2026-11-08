#!/usr/bin/env bash
# One-time setup for running the camera app on an Android phone under Termux.
# Install Termux from F-Droid (NOT the Play Store build). Then:
#   pkg install -y git
#   git clone <your-repo-url> camera && cd camera
#   bash deploy/termux-setup.sh
# After this, see DEPLOY.md for .env, rclone config, Tailscale, and running it.
set -eu

GO2RTC_VERSION="v1.9.15"   # match what was tested on the Mac

echo "==> Updating package lists"
pkg update -y && pkg upgrade -y

echo "==> Installing system packages (python, ffmpeg, rclone, numpy, procps, termux-api)"
# numpy from pkg is far faster than building the wheel; procps gives pkill (run.sh uses it);
# termux-api is for termux-wake-lock so the phone doesn't sleep the app.
pkg install -y python ffmpeg rclone procps termux-api python-numpy

echo "==> Creating a virtualenv and installing the pure-Python deps"
# A venv avoids Termux's PEP 668 "externally-managed-environment" pip block, and
# --system-site-packages lets it see the pkg-installed numpy (no slow numpy build).
# run.sh prefers .venv/bin/python automatically. The web stack is Starlette-based
# (no FastAPI/pydantic/Rust), so everything here is pure Python — no compilation.
python -m venv --system-site-packages .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install python-dotenv starlette uvicorn websockets itsdangerous

echo "==> Checking ffmpeg has libx264 (needed to re-encode clips)"
if ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libx264; then
  echo "    libx264: OK"
else
  echo "    WARNING: libx264 not found in ffmpeg — clip re-encoding will fail." >&2
fi

echo "==> Fetching the go2rtc binary ($GO2RTC_VERSION)"
mkdir -p tools/bin
case "$(uname -m)" in
  aarch64|arm64) GOARCH="arm64" ;;
  armv7l|armv8l|arm) GOARCH="arm" ;;
  x86_64) GOARCH="amd64" ;;
  *) echo "    Unknown arch $(uname -m); download go2rtc manually to tools/bin/go2rtc" >&2; GOARCH="" ;;
esac
if [ -n "$GOARCH" ]; then
  url="https://github.com/AlexxIT/go2rtc/releases/download/${GO2RTC_VERSION}/go2rtc_linux_${GOARCH}"
  echo "    $url"
  rclone --version >/dev/null 2>&1 || true
  if command -v curl >/dev/null 2>&1; then curl -fL "$url" -o tools/bin/go2rtc;
  else pkg install -y curl && curl -fL "$url" -o tools/bin/go2rtc; fi
  chmod +x tools/bin/go2rtc
  echo "    go2rtc: $(tools/bin/go2rtc --version 2>&1 | head -1 || echo 'installed')"
fi

echo
echo "==> Packages done. Next (see DEPLOY.md):"
echo "    1. cp .env.example .env   and fill RTSP_MAIN_URL, WEB_PASSWORD, NAS_REMOTE=nas:Recordings"
echo "    2. rclone config          (recreate the 'nas' SMB remote: host <nas-ip>, your creds)"
echo "    3. Install the Tailscale app and sign in (same tailnet as the NAS)"
echo "    4. termux-wake-lock && bash run.sh"
