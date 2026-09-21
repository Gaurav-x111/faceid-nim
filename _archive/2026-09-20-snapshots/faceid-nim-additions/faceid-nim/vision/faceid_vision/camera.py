"""Camera sources: V4L2 via OpenCV, plus a file source for CI replay."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterator

import cv2
import numpy as np


@dataclass
class CameraConfig:
    device: str = "/dev/video0"       # or an int index, or a path to a video file
    width: int = 640
    height: int = 480
    fps: int = 30
    warmup_frames: int = 6            # discarded while auto-exposure settles
    fourcc: str | None = "MJPG"
    is_file: bool = False


class CameraError(RuntimeError):
    pass


class Camera:
    """Opens on enter, closes on exit.

    The camera stays shut until a scan is triggered so the privacy LED
    means something.
    """

    def __init__(self, cfg: CameraConfig):
        self.cfg = cfg
        self.cap: cv2.VideoCapture | None = None

    def __enter__(self) -> "Camera":
        src: str | int = self.cfg.device
        if not self.cfg.is_file:
            try:
                src = int(str(self.cfg.device).rsplit("video", 1)[-1])
            except ValueError:
                src = self.cfg.device
        cap = cv2.VideoCapture(src)
        if not cap.isOpened():
            raise CameraError(f"cannot open camera source {self.cfg.device!r}")
        if not self.cfg.is_file:
            if self.cfg.fourcc:
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.cfg.fourcc))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.height)
            cap.set(cv2.CAP_PROP_FPS, self.cfg.fps)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            for _ in range(self.cfg.warmup_frames):
                cap.read()
        self.cap = cap
        return self

    def __exit__(self, *exc) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def frames(self, timeout_s: float) -> Iterator[tuple[float, np.ndarray]]:
        """Yield (monotonic_timestamp, BGR frame) until timeout or EOF."""
        if self.cap is None:
            raise CameraError("camera not open")
        t0 = time.monotonic()
        while True:
            now = time.monotonic()
            if now - t0 > timeout_s:
                return
            ok, frame = self.cap.read()
            if not ok:
                return                      # file EOF, or the device went away
            yield now, frame


def open_source(device: str, **kw) -> Camera:
    is_file = not str(device).startswith("/dev/") and not str(device).isdigit()
    return Camera(CameraConfig(device=device, is_file=is_file, **kw))
