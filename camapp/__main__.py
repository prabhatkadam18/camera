"""Recording + motion detection + clipping.

Runs alongside the web UI (camapp.web) and go2rtc: continuously records the stream
into a rolling buffer, watches for motion, and saves/syncs a clip around each event.
"""
from __future__ import annotations

import logging
import signal
import sys
import threading
import time

from .buffer import prune
from .config import load_config
from .events import Clipper, prune_clips, prune_nas
from .motion_ff import MotionDetector, MotionEvent
from .recorder import Recorder

log = logging.getLogger("camapp")

PRUNE_INTERVAL_SECONDS = 5


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    cfg.buffer_dir.mkdir(parents=True, exist_ok=True)

    clipper = Clipper(cfg)

    def on_start(event: MotionEvent) -> None:
        log.info("Motion started (%.1f%%)", event.peak_area * 100)

    def on_end(event: MotionEvent) -> None:
        log.info("Motion ended after %.1fs (peak %.1f%%)", event.last_motion - event.started, event.peak_area * 100)
        clipper.on_motion_end(event)

    recorder = Recorder(cfg.record_url, cfg.buffer_dir, cfg.segment_seconds, cfg.rtsp_transport)
    detector = MotionDetector(cfg.motion_url, cfg.rtsp_transport, cfg.motion, on_start, on_end)
    stop = threading.Event()

    def shutdown(*_):
        if not stop.is_set():
            log.info("Shutting down")
        stop.set()
        recorder.stop()
        detector.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    threads = [
        threading.Thread(target=recorder.run, name="recorder"),
        threading.Thread(target=detector.run, name="motion"),
    ]
    for t in threads:
        t.start()
    log.info("Recording %s to %s; motion on %s; clips to %s",
             cfg.record_url.split("@")[-1], cfg.buffer_dir, cfg.motion_url.split("@")[-1], cfg.clips_dir)

    max_age = cfg.buffer_minutes * 60
    CLIP_PRUNE_EVERY = 600        # prune local saved clips every 10 min
    NAS_PRUNE_EVERY = 6 * 3600    # prune the NAS every 6 h
    last_clip = last_nas = -1e9   # prune once at startup too
    while all(t.is_alive() for t in threads) and not stop.wait(PRUNE_INTERVAL_SECONDS):
        prune(cfg.buffer_dir, max_age)  # rolling buffer (minutes)
        now = time.monotonic()
        if now - last_clip >= CLIP_PRUNE_EVERY:
            last_clip = now
            n = prune_clips(cfg.clips_dir, cfg.clip_retention_days)
            if n:
                log.info("Pruned %d local clip(s) older than %.0fd", n, cfg.clip_retention_days)
        if now - last_nas >= NAS_PRUNE_EVERY:
            last_nas = now  # rclone is network I/O, so prune the NAS off the loop thread
            threading.Thread(target=prune_nas, args=(cfg, cfg.nas_retention_days), daemon=True).start()

    crashed = not stop.is_set()
    if crashed:
        log.error("A worker thread exited unexpectedly, stopping")
    shutdown()
    for t in threads:
        t.join()
    sys.exit(1 if crashed else 0)


if __name__ == "__main__":
    main()
