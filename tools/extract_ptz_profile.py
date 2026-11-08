"""Build data/ptz_profile.json from a PCAPdroid capture of the Yoosee app panning.

Capture (PCAPdroid, per-app, no root): record the Yoosee app while pressing pan
LEFT a few times then RIGHT a few times, then export the .pcap.

Usage: python tools/extract_ptz_profile.py <capture.pcap> [--left-sig HEX] [--right-sig HEX]

Direction signatures default to the values decoded for the reference camera; on a
different unit, re-derive them by diffing the command packets (see README).
"""
from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CMD_PREFIX = "c010030b"       # the control-command packet type
DIR_OFFSET = 58               # 8-byte direction block inside the command body
SESSION_CUTOFF_S = 12.0       # replay handshake/keepalive packets before this time


def read_pcap(path: Path):
    d = path.read_bytes()
    off = 24
    while off + 16 <= len(d):
        _, usec, caplen, _ = struct.unpack("<IIII", d[off:off + 16])
        off += 16
        yield usec, d[off:off + caplen]
        off += caplen


def udp(raw: bytes):
    if len(raw) < 20 or (raw[0] >> 4) != 4 or raw[9] != 17:
        return None
    ihl = (raw[0] & 0xf) * 4
    src = ".".join(map(str, raw[12:16]))
    dst = ".".join(map(str, raw[16:20]))
    sp, dp, ln, _ = struct.unpack(">HHHH", raw[ihl:ihl + 8])
    return src, sp, dst, dp, raw[ihl + 8:ihl + ln]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pcap", type=Path)
    # 8-byte encrypted direction blocks (offset 58 of a c010030b command), hex.
    # These are session-key-specific: they must come from the SAME capture as the handshake.
    ap.add_argument("--left-sig", required=True)
    ap.add_argument("--right-sig", required=True)
    ap.add_argument("--up-sig")
    ap.add_argument("--down-sig")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "ptz_profile.json")
    args = ap.parse_args()

    pkts = []
    for _, raw in read_pcap(args.pcap):
        r = udp(raw)
        if r:
            pkts.append(r)
    # The camera is whichever host receives the command packets on a fixed port.
    cam = port = None
    for src, sp, dst, dp, pl in pkts:
        if pl[:4].hex() == CMD_PREFIX:
            cam, port = dst, dp
            break
    if cam is None:
        raise SystemExit("No command packets found; capture while pressing the pan buttons.")

    tocam = [pl for src, sp, dst, dp, pl in pkts if dst == cam and dp == port and pl]
    src_port = next((sp for src, sp, dst, dp, pl in pkts if dst == cam and dp == port), 0)
    base = None
    timed = []
    for usec, raw in read_pcap(args.pcap):
        r = udp(raw)
        if not r or r[2] != cam or r[3] != port or not r[4]:
            continue
        if base is None:
            base = usec
        timed.append(((usec - base) / 1e6, r[4]))

    is_cmd = lambda pl: pl[:4].hex() == CMD_PREFIX
    session = [[round(t, 4), pl.hex()] for t, pl in timed if t < SESSION_CUTOFF_S and not is_cmd(pl)]
    carriers = [pl.hex() for t, pl in timed if is_cmd(pl)]

    sigs = {"left": args.left_sig, "right": args.right_sig}
    if args.up_sig:
        sigs["up"] = args.up_sig
    if args.down_sig:
        sigs["down"] = args.down_sig
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "cam_ip": cam, "port": port, "src_port": src_port, "dir_offset": DIR_OFFSET,
        "signatures": sigs,
        "carriers": carriers, "session": session,
    }))
    print(f"wrote {args.out}: cam={cam}:{port} session={len(session)} carriers={len(carriers)}")


if __name__ == "__main__":
    main()
