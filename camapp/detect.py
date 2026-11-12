"""Person/animal detector — the second stage of a motion gate.

Background-subtraction motion is cheap but fires on anything that changes (lights,
shadows, headlights, rain). This runs a small YOLO model (Ultralytics) on a single
snapshot from go2rtc ONLY when motion triggers, and reports whether a person or animal
is actually in frame. The recorder then saves a clip only when one is.

Designed to fail OPEN and degrade gracefully: if the model or its runtime isn't
installed (e.g. on the phone/Termux), the detector reports "unavailable" and the app
falls back to plain motion — recording is never blocked by a broken detector. Heavy
deps (ultralytics/torch) live in requirements-detect.txt, installed only where wanted
(the Raspberry Pi), not in the base requirements.
"""
from __future__ import annotations

import io
import logging
import urllib.request
from typing import Optional

log = logging.getLogger("detect")

# COCO class ids. Ultralytics YOLOv8 uses the standard COCO order.
PERSON = {0}
ANIMALS = {14, 15, 16, 17, 18, 19, 20, 21, 22, 23}  # bird,cat,dog,horse,sheep,cow,elephant,bear,zebra,giraffe


def _snapshot(go2rtc_url: str, src: str, timeout: float) -> Optional[bytes]:
    """Grab one JPEG frame from go2rtc's live stream (no extra camera pull)."""
    url = f"{go2rtc_url.rstrip('/')}/api/frame.jpeg?src={src}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read()
    except Exception as e:  # noqa: BLE001
        log.warning("Snapshot fetch failed (%s)", e)
        return None


class Detector:
    def __init__(self, model_path: str = "yolov8n.pt", conf: float = 0.4,
                 targets: Optional[set[int]] = None, snapshot_timeout: float = 5.0):
        self.conf = conf
        self.targets = targets if targets is not None else (PERSON | ANIMALS)
        self.snapshot_timeout = snapshot_timeout
        self._model = None
        self._Image = None
        try:
            from ultralytics import YOLO
            from PIL import Image
            self._model = YOLO(model_path)
            self._Image = Image
            log.info("Detector ready (%s, conf=%.2f, targets=%s)", model_path, conf, sorted(self.targets))
        except Exception as e:  # noqa: BLE001 — runtime/model not installed
            log.warning("Detector unavailable (%s) — using plain motion (no person/animal filter)", e)

    @property
    def ready(self) -> bool:
        return self._model is not None

    def _has_target(self, jpeg: bytes) -> bool:
        img = self._Image.open(io.BytesIO(jpeg)).convert("RGB")
        for r in self._model.predict(img, conf=self.conf, verbose=False):
            for c in r.boxes.cls.tolist():
                if int(c) in self.targets:
                    return True
        return False

    def targets_present(self, go2rtc_url: str, src: str = "camera") -> bool:
        """True if a person/animal is in the current frame. Fails OPEN (returns True) when
        the detector is unavailable or a frame/inference can't be obtained, so a real event
        is never dropped because of a detector problem."""
        if self._model is None:
            return True
        jpeg = _snapshot(go2rtc_url, src, self.snapshot_timeout)
        if jpeg is None:
            return True
        try:
            return self._has_target(jpeg)
        except Exception as e:  # noqa: BLE001
            log.warning("Inference failed (%s) — allowing the event", e)
            return True
