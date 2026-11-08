from __future__ import annotations

import logging
import os
import select
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

import cv2
import numpy as np

from . import ffmpeg
from .config import MotionSettings, Rect

log = logging.getLogger("motion")

FRAME_W, FRAME_H = 320, 180
FRAME_BYTES = FRAME_W * FRAME_H
STALL_SECONDS = 10
WARMUP_SECONDS = 3
# Above this share of changed pixels the change is lighting (night-mode switch, lights on), not motion.
GLOBAL_CHANGE_LIMIT = 0.5


@dataclass
class MotionEvent:
    started: float
    last_motion: float
    peak_area: float


def build_mask(masks: List[Rect]) -> np.ndarray:
    mask = np.full((FRAME_H, FRAME_W), 255, np.uint8)
    for x, y, w, h in masks:
        x0, y0 = int(x * FRAME_W), int(y * FRAME_H)
        x1, y1 = int((x + w) * FRAME_W + 0.5), int((y + h) * FRAME_H + 0.5)
        mask[y0:y1, x0:x1] = 0
    return mask


class MotionAnalyzer:
    def __init__(self, settings: MotionSettings):
        self.subtractor = cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=25, detectShadows=False)
        self.mask = build_mask(settings.masks)
        self.watched_pixels = max(cv2.countNonZero(self.mask), 1)
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self.warmup_frames = settings.fps * WARMUP_SECONDS
        self.frames = 0

    def process(self, gray: np.ndarray) -> Optional[float]:
        """Share of watched pixels that moved, or None while the background model is (re)learning."""
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        fg = self.subtractor.apply(blurred)
        self.frames += 1
        if self.frames <= self.warmup_frames:
            return None
        fg = cv2.bitwise_and(fg, self.mask)
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, self.kernel)
        area = cv2.countNonZero(fg) / self.watched_pixels
        if area > GLOBAL_CHANGE_LIMIT:
            log.info("Whole-image change (%.0f%%), relearning background", area * 100)
            self.subtractor.apply(blurred, learningRate=1.0)
            self.frames = 0
            return None
        return area


def read_exact(stream, size: int, timeout: float, stop: threading.Event) -> Optional[bytes]:
    fd = stream.fileno()
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while len(buf) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or stop.is_set():
            return None
        ready, _, _ = select.select([fd], [], [], min(remaining, 1.0))
        if ready:
            chunk = os.read(fd, size - len(buf))
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
        self.settings = settings
        self.on_start = on_start
        self.on_end = on_end
        self._stop = threading.Event()
        self._event: Optional[MotionEvent] = None
        self._hits = 0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        ffmpeg.run_with_restarts(self._session, self._stop, log)
        log.info("Motion detector stopped")

    def _command(self) -> list:
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            *ffmpeg.input_args(self.url, self.rtsp_transport),
            "-an",
            "-vf", f"fps={self.settings.fps},scale={FRAME_W}:{FRAME_H},format=gray",
            "-f", "rawvideo", "pipe:1",
        ]

    def _session(self) -> None:
        log.info("Watching %s", ffmpeg.mask_credentials(self.url))
        proc = ffmpeg.start(self._command(), log, stdout=subprocess.PIPE)
        analyzer = MotionAnalyzer(self.settings)
        try:
            while not self._stop.is_set():
                data = read_exact(proc.stdout, FRAME_BYTES, STALL_SECONDS, self._stop)
                if data is None:
                    if not self._stop.is_set():
                        log.warning("No frames for %ss or stream ended", STALL_SECONDS)
                    break
                frame = np.frombuffer(data, np.uint8).reshape(FRAME_H, FRAME_W)
                self._update(analyzer.process(frame), time.time())
        finally:
            self._finish_event()
            ffmpeg.stop(proc)

    def _update(self, area: Optional[float], now: float) -> None:
        moving = area is not None and area >= self.settings.min_area
        self._hits = self._hits + 1 if moving else 0
        event = self._event
        if event is None:
            if self._hits >= self.settings.min_frames:
                self._event = MotionEvent(started=now, last_motion=now, peak_area=area)
                self.on_start(self._event)
        elif moving:
            event.last_motion = now
            event.peak_area = max(event.peak_area, area)
        elif now - event.last_motion >= self.settings.end_seconds:
            self._finish_event()

    def _finish_event(self) -> None:
        if self._event is not None:
            self.on_end(self._event)
            self._event = None
        self._hits = 0
