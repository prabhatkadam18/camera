"""Pan/tilt control for the camera over its LAN P2P channel (UDP 44342).

The vendor firmware (33.x) exposes no working ONVIF or RTSP PTZ. The app drives the
camera with a proprietary UDP protocol that we reproduce from a profile built by
replaying one captured session:

- open a session by replaying the captured handshake (static per device),
- keep it alive with channel-5200 heartbeats in a background thread,
- send channel-5100 command packets whose 8-byte direction block is the captured
  (encrypted) value for that direction, with a running command counter we generate
  ourselves. The firmware ignores the packet checksum, so we only need a valid,
  contiguous counter, not the real checksum or the encryption key.

Hold one session open (connect once, ~12s handshake replay) and pan as often as you
like with no further wait. Only left/right are known until up/down blocks are captured.
"""
from __future__ import annotations

import json
import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = ROOT / "data" / "ptz_profile.json"
log = logging.getLogger("ptz")

DIRECTIONS = ("left", "right", "up", "down")

# c010030b command body: counter (continues the session's count; handshake ends at 3)
# and ack (previous counter), both little-endian uint32 cleartext.
COUNTER_OFF = 18
ACK_OFF = 22
FIRST_COUNTER = 4
STEP_GAP_S = 0.25           # spacing between command packets within one pan
KEEPALIVE_S = 1.0           # channel-5200 heartbeat interval
HEARTBEAT_PREFIX = "c0100603"


@dataclass
class Profile:
    cam_ip: str
    port: int
    src_port: int
    dir_offset: int
    signatures: dict[str, str]
    carriers: list[bytes]
    session: list[tuple[float, bytes]]

    @classmethod
    def load(cls, path: Path = DEFAULT_PROFILE) -> "Profile":
        if not path.exists():
            raise FileNotFoundError(
                f"No PTZ profile at {path}. Build one from a capture with tools/extract_ptz_profile.py."
            )
        d = json.loads(path.read_text())
        return cls(
            cam_ip=d["cam_ip"], port=d["port"], src_port=d.get("src_port", 0),
            dir_offset=d["dir_offset"], signatures=d["signatures"],
            carriers=[bytes.fromhex(h) for h in d["carriers"]],
            session=[(float(t), bytes.fromhex(h)) for t, h in d["session"]],
        )


class PTZController:
    """Holds one live session. connect() once, then pan() as often as you like."""

    def __init__(self, profile: Profile | None = None):
        self.p = profile or Profile.load()
        self._heartbeats = [pl for _, pl in self.p.session if pl[:4].hex() == HEARTBEAT_PREFIX]
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()  # serializes connect/close/pan
        self._counter = FIRST_COUNTER

    # -- lifecycle ---------------------------------------------------------
    def connect(self) -> None:
        with self._lock:
            self._connect_locked()

    def _connect_locked(self) -> None:
        if self._sock is not None:
            return
        stop = threading.Event()        # per-connection; old threads keep their own
        self._stop = stop
        self._counter = FIRST_COUNTER
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Use a FRESH random source port each time: the camera clings to a dropped
        # session for ~30s, so reconnecting from a new port gets a clean slot instead
        # of colliding with the old session on the same port.
        s.bind(("", 0))
        self._sock = s
        threading.Thread(target=self._drain, args=(s, stop), daemon=True).start()

        log.info("Opening session to %s (replaying handshake)", self.p.cam_ip)
        try:
            start = time.monotonic()
            for t, pl in self.p.session:
                dt = t - (time.monotonic() - start)
                if dt > 0:
                    time.sleep(dt)
                s.sendto(pl, (self.p.cam_ip, self.p.port))
        except OSError as e:
            log.error("Handshake failed: %s", e)
            stop.set()
            s.close()
            self._sock = None
            return
        threading.Thread(target=self._keepalive, args=(s, stop), daemon=True).start()
        log.info("Session live; ready to pan")

    def _teardown_locked(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def close(self) -> None:
        with self._lock:
            self._teardown_locked()

    def __enter__(self) -> "PTZController":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- control -----------------------------------------------------------
    def pan(self, direction: str, steps: int = 1) -> None:
        if direction not in self.p.signatures:
            raise ValueError(f"No capture for '{direction}'. Known: {sorted(self.p.signatures)}")
        if self._sock is None:
            self.connect()
        with self._lock:
            if self._sock is None:
                raise RuntimeError("PTZ session not available (handshake failed)")
            sig = bytes.fromhex(self.p.signatures[direction])
            off = self.p.dir_offset
            template = self.p.carriers[0]
            for _ in range(max(1, steps)):
                n = self._counter
                self._counter += 1
                b = bytearray(template)
                b[COUNTER_OFF:COUNTER_OFF + 4] = struct.pack("<I", n)
                b[ACK_OFF:ACK_OFF + 4] = struct.pack("<I", n - 1)
                b[off:off + len(sig)] = sig
                self._sock.sendto(bytes(b), (self.p.cam_ip, self.p.port))
                time.sleep(STEP_GAP_S)
        log.info("Panned %s x%d", direction, steps)

    # -- background --------------------------------------------------------
    def _drain(self, sock: socket.socket, stop: threading.Event) -> None:
        sock.settimeout(0.3)
        while not stop.is_set():
            try:
                sock.recvfrom(4096)
            except OSError:
                if stop.is_set():
                    break

    def _keepalive(self, sock: socket.socket, stop: threading.Event) -> None:
        i = 0
        while not stop.is_set():
            if self._heartbeats:
                try:
                    sock.sendto(self._heartbeats[i % len(self._heartbeats)], (self.p.cam_ip, self.p.port))
                except OSError:
                    break
                i += 1
            stop.wait(KEEPALIVE_S)


def main() -> None:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Pan the camera over its LAN P2P channel.")
    ap.add_argument("actions", nargs="+", help="e.g. left left right, or 'right:3'")
    args = ap.parse_args()

    with PTZController() as ctrl:
        for a in args.actions:
            name, _, n = a.partition(":")
            ctrl.pan(name, int(n) if n else 1)
            time.sleep(0.5)


if __name__ == "__main__":
    main()
