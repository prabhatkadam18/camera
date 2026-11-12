"""Turn a motion event into a saved clip: cut the matching segments out of the
rolling buffer (pre-roll + motion + post-roll), join them, make a thumbnail, and
sync to the NAS over SMB via rclone."""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from .buffer import SEGMENT_EXT
from .config import Config

log = logging.getLogger("events")


def _seg_epoch(path: Path) -> float | None:
    try:
        return datetime.strptime(path.stem, "%Y%m%d-%H%M%S").timestamp()
    except ValueError:
        return None


class Clipper:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        cfg.clips_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last_clip_end = 0.0  # epoch of the last clip's window end, to avoid overlap

    def on_motion_end(self, event) -> None:
        """Called when a motion event ends; builds + syncs the clip in a thread so
        detection isn't blocked. Waits out the post-roll first so those segments exist."""
        threading.Thread(target=self._build_and_sync, args=(event,), daemon=True).start()

    def _build_and_sync(self, event) -> None:
        # Wait until the post-roll segments have been written to the buffer.
        time.sleep(self.cfg.event_post_seconds + self.cfg.segment_seconds + 1)
        clip = self.build_clip(event.started, event.last_motion)
        if clip:
            sync_to_nas(clip, self.cfg)

    def build_clip(self, start_ts: float, end_ts: float) -> Path | None:
        win_start = start_ts - self.cfg.event_pre_seconds
        win_end = end_ts + self.cfg.event_post_seconds
        # Don't re-include footage already saved in the previous clip. A long motion
        # that the duration cap split, or a quick re-trigger, would otherwise produce
        # overlapping clips of the same scene.
        with self._lock:
            if win_start < self._last_clip_end:
                win_start = self._last_clip_end
            if win_end <= win_start:
                log.info("Event at %s already covered by the previous clip; skipping",
                         datetime.fromtimestamp(start_ts))
                return None
            self._last_clip_end = win_end
        seglen = self.cfg.segment_seconds
        segs = []
        for p in self.cfg.buffer_dir.glob(f"*{SEGMENT_EXT}"):
            t = _seg_epoch(p)
            if t is not None and t < win_end and (t + seglen) > win_start:
                segs.append((t, p))
        segs.sort()
        if not segs:
            log.warning("No buffer segments for event at %s", datetime.fromtimestamp(start_ts))
            return None

        name = datetime.fromtimestamp(start_ts).strftime("EVENT_%Y%m%d-%H%M%S")
        out = self.cfg.clips_dir / f"{name}.mp4"
        listfile = self.cfg.clips_dir / f".{name}.txt"
        listfile.write_text("".join(f"file '{p.resolve()}'\n" for _, p in segs))
        try:
            # Re-encode rather than -c copy: the weak Wi-Fi link leaves corrupt frames and
            # multi-second gaps in the segments, which a stream copy carries straight into the
            # clip so a browser sticks on them. Re-decoding conceals/drops bad frames and a
            # constant frame rate fills gaps with a held frame, so the clip always plays
            # through. (Tune CLIP_PRESET/CLIP_FPS below if the phone CPU struggles.)
            CLIP_PRESET, CLIP_CRF, CLIP_FPS = "veryfast", "23", "15"
            r = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-err_detect", "ignore_err", "-fflags", "+genpts",
                 "-f", "concat", "-safe", "0", "-i", str(listfile),
                 "-c:v", "libx264", "-preset", CLIP_PRESET, "-crf", CLIP_CRF, "-pix_fmt", "yuv420p",
                 "-fps_mode", "cfr", "-r", CLIP_FPS,
                 "-c:a", "aac", "-b:a", "64k",
                 "-movflags", "+faststart", str(out)],
                capture_output=True, text=True, timeout=300,
            )
        finally:
            listfile.unlink(missing_ok=True)
        if r.returncode != 0 or not out.exists():
            log.error("Clip build failed: %s", r.stderr[-300:])
            return None
        self._thumbnail(out)
        log.info("Saved clip %s (%d segments)", out.name, len(segs))
        return out

    def _thumbnail(self, clip: Path) -> None:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(clip),
             "-frames:v", "1", "-vf", "scale=320:-1", str(clip.with_suffix(".jpg"))],
            capture_output=True, timeout=30,
        )


def prune_clips(clips_dir: Path, max_age_days: float) -> int:
    """Delete saved clips (and their thumbnails) older than max_age_days from this device.
    The phone keeps a short window; the NAS is the long-term store."""
    if max_age_days <= 0:
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for p in clips_dir.glob("EVENT_*.mp4"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
                p.with_suffix(".jpg").unlink(missing_ok=True)
                removed += 1
        except FileNotFoundError:
            continue
    return removed


def prune_nas(cfg: Config, max_age_days: float) -> None:
    """Delete clips older than max_age_days on the NAS via rclone (top level only, so it
    never recurses into the Synology #recycle folder). No-op if the NAS isn't configured."""
    if not cfg.nas_remote or max_age_days <= 0:
        return
    r = subprocess.run(
        ["rclone", "delete", "--min-age", f"{int(max_age_days)}d", "--max-depth", "1", cfg.nas_remote],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        log.warning("NAS retention prune failed: %s", r.stderr.strip()[:200])
    else:
        log.info("NAS retention prune done (older than %sd)", int(max_age_days))


def sync_to_nas(path: Path, cfg: Config, retries: int = 3) -> bool:
    """Copy the clip (and its thumbnail) to the NAS via rclone. No-op if not configured."""
    if not cfg.nas_remote:
        return False
    files = [path]
    thumb = path.with_suffix(".jpg")
    if thumb.exists():
        files.append(thumb)
    for f in files:
        for attempt in range(retries):
            r = subprocess.run(["rclone", "copyto", str(f), f"{cfg.nas_remote}/{f.name}"],
                               capture_output=True, text=True)
            if r.returncode == 0:
                break
            log.warning("rclone attempt %d for %s failed: %s", attempt + 1, f.name, r.stderr.strip()[:200])
            time.sleep(5)
        else:
            log.error("Gave up syncing %s to NAS", f.name)
            return False
    log.info("Synced %s to %s", path.name, cfg.nas_remote)
    return True
