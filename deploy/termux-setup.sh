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

echo "==> Building go2rtc from source ($GO2RTC_VERSION)"
# IMPORTANT: the prebuilt go2rtc Linux binary crashes on Android's seccomp
# (SIGSYS on the faccessat2 syscall when it looks up ffmpeg). So build it with
# Termux's Go, which is patched for Android's syscalls. The -checklinkname=0
# linker flag is required for the anet dependency's //go:linkname under Go 1.23+
# (see https://github.com/wlynxg/anet README).
pkg install -y golang
mkdir -p tools/bin
LDFLAGS="-checklinkname=0"
if ! GOBIN="$PWD/tools/bin" GOFLAGS=-buildvcs=false go install -ldflags="$LDFLAGS" "github.com/AlexxIT/go2rtc@${GO2RTC_VERSION}"; then
  echo "    ${GO2RTC_VERSION} failed to build; trying master…" >&2
  GOBIN="$PWD/tools/bin" GOFLAGS=-buildvcs=false go install -ldflags="$LDFLAGS" github.com/AlexxIT/go2rtc@master
fi
echo "    go2rtc: $(tools/bin/go2rtc --version 2>&1 | head -1 || echo 'build failed')"

echo
echo "==> Packages done. Next (see DEPLOY.md):"
echo "    1. cp .env.example .env   and fill RTSP_MAIN_URL, WEB_PASSWORD, NAS_REMOTE=nas:Recordings"
echo "    2. rclone config          (recreate the 'nas' SMB remote: host <nas-ip>, your creds)"
echo "    3. Install the Tailscale app and sign in (same tailnet as the NAS)"
echo "    4. termux-wake-lock && bash run.sh"
