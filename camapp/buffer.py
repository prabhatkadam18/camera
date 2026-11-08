from __future__ import annotations

import os
import time
from pathlib import Path

SEGMENT_EXT = ".ts"


def newest_mtime(directory: Path) -> float | None:
    newest: float | None = None
    for e in os.scandir(directory):
        if not e.name.endswith(SEGMENT_EXT):
            continue
        try:
            m = e.stat().st_mtime
        except FileNotFoundError:
            continue  # pruned between scandir and stat; ignore
        if newest is None or m > newest:
            newest = m
    return newest


def prune(directory: Path, max_age_seconds: float) -> int:
    cutoff = time.time() - max_age_seconds
    removed = 0
    for entry in os.scandir(directory):
        if not entry.name.endswith(SEGMENT_EXT):
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                os.remove(entry.path)
                removed += 1
        except FileNotFoundError:
            continue  # already gone; nothing to prune
    return removed
