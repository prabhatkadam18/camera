from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path

from . import ffmpeg
from .buffer import SEGMENT_EXT, newest_mtime

log = logging.getLogger("recorder")


def build_command(url: str, out_dir: Path, segment_seconds: int, rtsp_transport: str) -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        *ffmpeg.input_args(url, rtsp_transport),
        "-map", "0:v", "-map", "0:a?",
        "-c:v", "copy",
        # Camera audio is often G.711, which MPEG-TS cannot carry, so audio alone is re-encoded.
        "-c:a", "aac", "-b:a", "64k",
        "-f", "segment",
        "-segment_time", str(segment_seconds),
        "-segment_format", "mpegts",
        "-reset_timestamps", "1",
        "-strftime", "1",
        str(out_dir / f"%Y%m%d-%H%M%S{SEGMENT_EXT}"),
    ]


class Recorder:
    def __init__(self, url: str, out_dir: Path, segment_seconds: int, rtsp_transport: str):
        self.url = url
        self.out_dir = out_dir
        self.segment_seconds = segment_seconds
        self.rtsp_transport = rtsp_transport
        # ffmpeg can hang without exiting when the stream stalls, so no new data means restart.
        self.stall_seconds = max(3 * segment_seconds, 15)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        ffmpeg.run_with_restarts(self._session, self._stop, log)
        log.info("Recorder stopped")

    def _session(self) -> None:
        log.info("Recording %s", ffmpeg.mask_credentials(self.url))
        cmd = build_command(self.url, self.out_dir, self.segment_seconds, self.rtsp_transport)
        proc = ffmpeg.start(cmd, log, stdout=subprocess.DEVNULL)
        started = time.time()
        while proc.poll() is None:
            if self._stop.wait(1):
                break
            last_write = max(newest_mtime(self.out_dir) or 0, started)
            if time.time() - last_write > self.stall_seconds:
                log.warning("No new video for %ss, restarting ffmpeg", self.stall_seconds)
                break
        ffmpeg.stop(proc)
