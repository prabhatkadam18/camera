from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from typing import Callable

MIN_BACKOFF = 2
MAX_BACKOFF = 30


def mask_credentials(text: str) -> str:
    return re.sub(r"//([^:/@\s]+):[^@\s]*@", r"//\1:***@", text)


def input_args(url: str, rtsp_transport: str) -> list[str]:
    args: list[str] = []
    if url.startswith("rtsp://"):
        # Local reads (go2rtc restream) are reliable over TCP; the camera itself needs UDP.
        transport = "tcp" if ("127.0.0.1" in url or "localhost" in url) else rtsp_transport
        # timeout is in microseconds.
        args += ["-rtsp_transport", transport, "-timeout", "5000000"]
    return args + ["-i", url]


def start(cmd: list[str], log: logging.Logger, stdout) -> subprocess.Popen:
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=stdout, stderr=subprocess.PIPE, bufsize=0)
    threading.Thread(target=_log_stderr, args=(proc, log), daemon=True).start()
    return proc


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        # ffmpeg 7 ignores SIGTERM while streaming unless stdin is attached; "q" is its clean stop.
        try:
            proc.stdin.write(b"q")
            proc.stdin.flush()
        except OSError:
            pass
        # A full stdout pipe would block ffmpeg before it can finish, so keep reading until it exits.
        if proc.stdout:
            threading.Thread(target=_drain, args=(proc.stdout,), daemon=True).start()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def run_with_restarts(session: Callable[[], None], stop_event: threading.Event, log: logging.Logger) -> None:
    backoff = MIN_BACKOFF
    while not stop_event.is_set():
        started = time.monotonic()
        try:
            session()
        except Exception:
            # A transient error (e.g. a file pruned mid-scan) must not kill the
            # thread and take the whole app down — log it and restart the session.
            log.exception("Session crashed, recovering")
        if stop_event.is_set():
            break
        if time.monotonic() - started > 60:
            backoff = MIN_BACKOFF
        log.warning("Stream lost, reconnecting in %ss", backoff)
        stop_event.wait(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF)


def _drain(stream) -> None:
    while stream.read(65536):
        pass


def _log_stderr(proc: subprocess.Popen, log: logging.Logger) -> None:
    for raw in proc.stderr:
        line = raw.decode(errors="replace").rstrip()
        if line:
            log.warning("ffmpeg: %s", mask_credentials(line))
