# Deploying on an Android phone (Termux)

The phone is the always-on "brain": it pulls the camera, serves the login-gated web UI,
runs motion detection, and pushes clips to the NAS. The Synology is storage-only; Tailscale
gives remote access. The camera stays on the home Wi-Fi, isolated from the internet.

## 0. Install Termux
Install **Termux from F-Droid** — https://f-droid.org/packages/com.termux/ — **not** the
Play Store build (that one is outdated and `pkg install` fails). Also install **Termux:Boot**
and **Termux:API** from F-Droid (used for auto-start and wake-lock).

## 1. Get the code and run setup
```bash
pkg install -y git
git clone <YOUR_REPO_URL> camera
cd camera
bash deploy/termux-setup.sh
```
This installs python, ffmpeg (with libx264), rclone, numpy, and **builds go2rtc from
source** (a few minutes — the prebuilt binary crashes on Android's seccomp, so it's
compiled with Termux's Go using `-checklinkname=0`).

## 2. Configure
**`.env`** — copy the example and fill it in:
```bash
cp .env.example .env
nano .env
```
Set at least:
- `RTSP_MAIN_URL` — the camera RTSP URL (same one that works in VLC), e.g.
  `rtsp://user:pass@<camera-ip>:554/onvif1`
- `WEB_PASSWORD` — the login password for the web UI
- `NAS_REMOTE=nas:Recordings`

**rclone** — recreate the `nas` SMB remote (same as on the Mac):
```bash
rclone config
# n -> name: nas -> storage: smb -> host: <nas-ip> -> user: <smb user>
# port: 445 -> pass: (type your SMB password) -> domain: (enter) -> advanced: n -> y -> q
```
Test it: `rclone lsd nas:Recordings` should list the share.

`data/ptz_profile.json` (pan/tilt replay data) is **not** in the repo — it's specific to your
camera. Copy it to the phone manually (like `.env`) to `~/camera/data/ptz_profile.json`, e.g.
via `rclone`, `scp`, or USB. Without it everything works except pan/tilt.

## 3. Tailscale (remote access)
Install the **Tailscale** app on the phone and sign in to the **same tailnet as the NAS**.
Note the phone's tailnet IP (looks like `100.x.y.z`). Members on the tailnet reach the web
UI at `http://100.x.y.z:8080` and log in. On the home Wi-Fi you can also use the phone's LAN IP.

## 4. Run it (and keep it running)
Foreground test first:
```bash
termux-wake-lock      # stop Android from sleeping the app
bash run.sh
```
Open `http://<phone-ip>:8080`, log in, confirm the stream, pan/tilt, and a motion clip.

**Auto-start on boot** (Termux:Boot): create `~/.termux/boot/camera.sh`:
```bash
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/camera.sh <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
cd ~/camera && bash run.sh >> ~/camera/run.log 2>&1
EOF
chmod +x ~/.termux/boot/camera.sh
```
Reboot once to confirm it comes back up. Logs go to `~/camera/run.log`.

## 5. Verify end to end
- **Stream + audio**: web UI plays, controls work.
- **Pan/tilt**: D-pad moves the camera.
- **Motion → clip**: walk through view; a clip appears in RECORDED EVENTS and plays through.
- **NAS sync**: the clip also lands in the `Recordings` share (`rclone lsf nas:Recordings`).
- **Remote**: all of the above over the Tailscale IP from off the home network.

## Updating later
```bash
cd ~/camera && git pull && bash deploy/termux-setup.sh   # re-run setup only if deps changed
```
Restart `run.sh` (it stops any previous instance first).

## Notes
- The camera serves only one stream well, so go2rtc does the single pull and the recorder +
  motion read its local restream. Don't point a second app at the camera directly.
- Weak Wi-Fi at the camera causes occasional clip gaps/corruption; clips are re-encoded so
  they still play through. The real fix is moving the camera closer to the router / adding an AP.
