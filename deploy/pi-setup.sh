#!/usr/bin/env bash
# One-time setup for running the camera app on a Raspberry Pi (64-bit Raspberry Pi OS).
# Unlike Termux, this is ordinary Debian Linux: the prebuilt go2rtc binary runs as-is,
# ML deps install from wheels, and systemd gives reliable auto-start.
#
#   git clone <your-repo-url> camera && cd camera
#   bash deploy/pi-setup.sh
# Then: fill .env, copy data/ptz_profile.json, `rclone config` the nas remote, install
# the systemd service (printed at the end), and set DETECT_ENABLED=true for detection.
set -eu

GO2RTC_VERSION="v1.9.15"

echo "==> System packages (ffmpeg, rclone, python venv, curl)"
sudo apt-get update
sudo apt-get install -y ffmpeg rclone python3-venv python3-pip curl

echo "==> Python virtualenv + base deps (pure-Python web stack + numpy)"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
# pip unpacks wheels in $TMPDIR. On Pi OS the default /tmp is a small RAM tmpfs (~2GB), and
# torch overflows it ("No space left on device") even with plenty of disk free — so use a
# temp dir on the real disk.
export TMPDIR="$PWD/.pip-tmp"
mkdir -p "$TMPDIR"
# --no-cache-dir avoids keeping the big wheels (esp. torch) in ~/.cache, which can fill a
# small SD card during install ("No space left on device").
.venv/bin/python -m pip install --no-cache-dir -r requirements.txt

echo "==> Detection deps (ultralytics + torch; large — needs a few GB free)"
.venv/bin/python -m pip install --no-cache-dir -r requirements-detect.txt
echo "    pre-fetching the YOLO model…"
.venv/bin/python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')" || \
  echo "    (model will download on first run instead)"

echo "==> go2rtc binary ($GO2RTC_VERSION) — prebuilt runs on Pi OS (glibc), no build needed"
mkdir -p tools/bin
case "$(uname -m)" in
  aarch64|arm64) GOARCH="arm64" ;;
  armv7l|armv6l) GOARCH="arm" ;;
  x86_64) GOARCH="amd64" ;;
  *) echo "    Unknown arch $(uname -m); download go2rtc manually to tools/bin/go2rtc" >&2; GOARCH="" ;;
esac
if [ -n "$GOARCH" ]; then
  curl -fL "https://github.com/AlexxIT/go2rtc/releases/download/${GO2RTC_VERSION}/go2rtc_linux_${GOARCH}" -o tools/bin/go2rtc
  chmod +x tools/bin/go2rtc
  echo "    go2rtc: $(tools/bin/go2rtc --version 2>&1 | head -1)"
fi

rm -rf "$TMPDIR"

cat <<NEXT

==> Packages done. Finish setup (see DEPLOY.md "Raspberry Pi"):
  1. cp .env.example .env   # set RTSP_MAIN_URL, WEB_PASSWORD, NAS_REMOTE=nas:Recordings, DETECT_ENABLED=true
  2. copy data/ptz_profile.json onto the Pi (not in the repo)
  3. rclone config         # recreate the 'nas' SMB remote
  4. install the service (fills in your user + this dir automatically):
     sed "s|<USER>|$USER|g; s|<DIR>|$PWD|g" deploy/camera.service | sudo tee /etc/systemd/system/camera.service >/dev/null
     sudo systemctl daemon-reload && sudo systemctl enable --now camera
  5. check it:  systemctl status camera   and   journalctl -u camera -f
NEXT
