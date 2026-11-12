from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
GO2RTC_RESTREAM = "rtsp://127.0.0.1:8554/camera"  # go2rtc's local restream of the single camera pull

Rect = Tuple[float, float, float, float]


@dataclass(frozen=True)
class MotionSettings:
    fps: int
    min_area: float
    min_frames: int
    end_seconds: float
    global_limit: float = 0.60  # whole-frame change above this is lighting/glitch, not motion
    max_event_seconds: float = 300.0  # force an event to end (and emit a clip) after this long
    masks: List[Rect] = field(default_factory=list)


@dataclass(frozen=True)
class Config:
    main_url: str
    sub_url: str
    record_url: str
    motion_url: str
    rtsp_transport: str
    buffer_dir: Path
    clips_dir: Path
    segment_seconds: int
    buffer_minutes: float
    clip_retention_days: float   # keep saved clips on THIS device (phone: short, limited space)
    nas_retention_days: float    # keep clips on the NAS (long-term store)
    motion: MotionSettings
    event_pre_seconds: float
    event_post_seconds: float
    web_port: int
    go2rtc_url: str
    nas_remote: str  # rclone remote path for clip upload, e.g. "nas:camera/clips" ("" disables)
    web_password: str
    secret_key: str


def _path(value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else ROOT / p


def _masks(value: str) -> List[Rect]:
    rects: List[Rect] = []
    for part in filter(None, (p.strip() for p in value.split(";"))):
        nums = [float(n) for n in part.split(",")]
        if len(nums) != 4 or not all(0 <= n <= 1 for n in nums):
            raise ValueError(f"MOTION_MASKS entry '{part}' must be four numbers 0..1: x,y,width,height")
        rects.append((nums[0], nums[1], nums[2], nums[3]))
    return rects


def load_config() -> Config:
    load_dotenv(ROOT / ".env")
    main_url = os.getenv("RTSP_MAIN_URL", "").strip()
    if not main_url:
        sys.exit("RTSP_MAIN_URL is not set. Copy .env.example to .env and fill it in.")
    try:
        motion = MotionSettings(
            fps=int(os.getenv("MOTION_FPS", "5")),
            min_area=float(os.getenv("MOTION_MIN_AREA", "0.005")),
            min_frames=int(os.getenv("MOTION_MIN_FRAMES", "3")),
            end_seconds=float(os.getenv("MOTION_END_SECONDS", "5")),
            global_limit=float(os.getenv("MOTION_GLOBAL_LIMIT", "0.60")),
            max_event_seconds=float(os.getenv("MOTION_MAX_EVENT_SECONDS", "300")),
            masks=_masks(os.getenv("MOTION_MASKS", "0,0,0.42,0.09")),
        )
    except ValueError as e:
        sys.exit(f"Invalid motion setting in .env: {e}")
    return Config(
        main_url=main_url,
        sub_url=os.getenv("RTSP_SUB_URL", "").strip() or main_url,
        # The cheap camera can only serve ONE stream well, so recording and motion
        # both read go2rtc's local restream by default (go2rtc does the single camera
        # pull). Override to a camera URL only if not using go2rtc.
        record_url=os.getenv("RECORD_URL", "").strip() or GO2RTC_RESTREAM,
        motion_url=os.getenv("MOTION_URL", "").strip() or GO2RTC_RESTREAM,
        rtsp_transport=os.getenv("RTSP_TRANSPORT", "udp").strip().lower(),
        buffer_dir=_path(os.getenv("BUFFER_DIR", "data/buffer")),
        clips_dir=_path(os.getenv("CLIPS_DIR", "data/clips")),
        segment_seconds=int(os.getenv("SEGMENT_SECONDS", "10")),
        buffer_minutes=float(os.getenv("BUFFER_MINUTES", "10")),
        clip_retention_days=float(os.getenv("CLIP_RETENTION_DAYS", "3")),
        nas_retention_days=float(os.getenv("NAS_RETENTION_DAYS", "30")),
        motion=motion,
        event_pre_seconds=float(os.getenv("EVENT_PRE_SECONDS", "6")),
        event_post_seconds=float(os.getenv("EVENT_POST_SECONDS", "6")),
        web_port=int(os.getenv("WEB_PORT", "8080")),
        go2rtc_url=os.getenv("GO2RTC_URL", "http://127.0.0.1:1984").strip(),
        nas_remote=os.getenv("NAS_REMOTE", "").strip(),
        web_password=os.getenv("WEB_PASSWORD", "").strip(),
        secret_key=_secret_key(),
    )


def _secret_key() -> str:
    """Stable signing key for session cookies; generated once into data/secret.key."""
    env = os.getenv("SECRET_KEY", "").strip()
    if env:
        return env
    path = ROOT / "data" / "secret.key"
    if path.exists():
        return path.read_text().strip()
    import secrets
    key = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key)
    return key
