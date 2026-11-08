"""Motion detection with a running-average background model (no OpenCV).

ffmpeg decodes the stream to small raw grayscale frames; we keep a slowly-adapting
background and count how many pixels differ from it. Counting changed *pixels* (not a
whole-frame average) detects a small or distant person reliably, while twinkle/noise
stays below the area threshold. Needs numpy (Termux: `pkg install python-numpy`).
Interface matches camapp.motion so the app can use either backend.
"""
from __future__ import annotations

import logging
import os
import select
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from . import ffmpeg
from .config import MotionSettings

log = logging.getLogger("motion")

FRAME_W, FRAME_H = 160, 90
FRAME_BYTES = FRAME_W * FRAME_H
STALL_SECONDS = 10
WARMUP_SECONDS = 4          # ignore frames right after (re)connect while the stream settles
PIXEL_THRESH = 22           # per-pixel grey-level difference to count a pixel as "changed"
BG_ALPHA = 0.02             # background adaptation rate per frame (slow)


@dataclass
class MotionEvent:
    started: float
    last_motion: float
    peak_area: float


def _drawbox_for_masks(masks) -> str:
    out = []
    for x, y, w, h in masks:
        out.append(f"drawbox=x=iw*{x}:y=ih*{y}:w=iw*{w}:h=ih*{h}:color=black:t=fill")
    return ",".join(out)


def read_frame(stream, timeout: float, stop: threading.Event) -> Optional[bytes]:
    fd = stream.fileno()
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while len(buf) < FRAME_BYTES:
        if stop.is_set() or time.monotonic() > deadline:
            return None
        ready, _, _ = select.select([fd], [], [], 1.0)
        if ready:
            chunk = os.read(fd, FRAME_BYTES - len(buf))
            if not chunk:
                return None
            buf += chunk
    return bytes(buf)


class MotionDetector:
    def __init__(
        self,
        url: str,
        rtsp_transport: str,
        settings: MotionSettings,
        on_start: Callable[[MotionEvent], None],
        on_end: Callable[[MotionEvent], None],
    ):
        self.url = url
        self.rtsp_transport = rtsp_transport
        self.s = settings
        self.on_start = on_start
        self.on_end = on_end
        self._stop = threading.Event()
        self._event: Optional[MotionEvent] = None
        self._hits = 0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        ffmpeg.run_with_restarts(self._session, self._stop, log)
        self._finish_event()
        log.info("Motion detector stopped")

    def _command(self) -> list:
        # Apply mask boxes BEFORE converting to gray — drawbox on gray8 corrupts the frame.
        vf = [f"fps={self.s.fps}", f"scale={FRAME_W}:{FRAME_H}"]
        box = _drawbox_for_masks(self.s.masks)
        if box:
            vf.append(box)
        vf.append("format=gray")
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-fflags", "discardcorrupt",  # drop corrupted frames so glitches aren't seen as motion
            *ffmpeg.input_args(self.url, self.rtsp_transport),
            "-an", "-vf", ",".join(vf), "-f", "rawvideo", "pipe:1",
        ]

    def _session(self) -> None:
        log.info("Watching %s (background-subtraction motion)", ffmpeg.mask_credentials(self.url))
        proc = ffmpeg.start(self._command(), log, stdout=subprocess.PIPE)
        bg = None
        prev_changed = None
        frames = 0
        warmup = max(5, self.s.fps * WARMUP_SECONDS)
        try:
            while not self._stop.is_set():
                raw = read_frame(proc.stdout, STALL_SECONDS, self._stop)
                if raw is None:
                    if not self._stop.is_set():
                        log.warning("No frames for %ss or stream ended", STALL_SECONDS)
                    break
                frame = np.frombuffer(raw, np.uint8).astype(np.float32)
                frames += 1
                if bg is None:
                    bg = frame.copy()
                    prev_changed = np.zeros(FRAME_BYTES, dtype=bool)
                    continue
                changed = np.abs(frame - bg) > PIXEL_THRESH
                # Count a pixel only if it changed in this frame AND the previous one.
                # A one-frame decode glitch (weak Wi-Fi: "error while decoding MB")
                # doesn't persist, so it's ignored; real motion survives.
                area = float(np.count_nonzero(changed & prev_changed)) / FRAME_BYTES
                prev_changed = changed
                # Adapt the background slowly toward the current frame.
                bg += BG_ALPHA * (frame - bg)
                if frames > warmup:
                    self._update(area, time.time(), frame, bg)
        finally:
            ffmpeg.stop(proc)

    def _update(self, area: float, now: float, frame, bg) -> None:
        if area > self.s.global_limit:
            # Whole-frame change (lights, night-mode IR switch): relearn instantly, don't alert.
            log.info("Whole-image change (%.0f%%), ignoring", area * 100)
            bg[:] = frame
            self._hits = 0
            return
        moving = area >= self.s.min_area
        self._hits = self._hits + 1 if moving else 0
        event = self._event
        if event is None:
            if self._hits >= self.s.min_frames:
                self._event = MotionEvent(now, now, area)
                self.on_start(self._event)
            return
        if moving:
            event.last_motion = now
            event.peak_area = max(event.peak_area, area)
        # End on a long enough quiet gap, OR cap the duration so a clip is always
        # produced and no single event (e.g. a glitchy stream) can run forever.
        if now - event.started >= self.s.max_event_seconds:
            self._finish_event()
        elif not moving and now - event.last_motion >= self.s.end_seconds:
            self._finish_event()

    def _finish_event(self) -> None:
        if self._event is not None:
            self.on_end(self._event)
            self._event = None
        self._hits = 0
