"""Blink detection from eye aspect ratio.

Needs dense landmarks (MediaPipe Face Mesh). YuNet's five points give
one point per eye, which cannot express eyelid closure at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# MediaPipe Face Mesh indices, in the classic six-point EAR order
# (outer corner, upper-1, upper-2, inner corner, lower-2, lower-1).
MP_LEFT_EYE = (33, 160, 158, 133, 153, 144)
MP_RIGHT_EYE = (362, 385, 387, 263, 373, 380)


def eye_aspect_ratio(pts6: np.ndarray) -> float:
    """EAR = (|p2-p6| + |p3-p5|) / (2 * |p1-p4|)."""
    p = np.asarray(pts6, dtype=np.float32).reshape(6, 2)
    a = np.linalg.norm(p[1] - p[5])
    b = np.linalg.norm(p[2] - p[4])
    c = np.linalg.norm(p[0] - p[3]) + 1e-6
    return float((a + b) / (2.0 * c))


@dataclass
class BlinkDetector:
    """A blink is a short dip below `closed_thr` that comes back up.

    A sustained low EAR is not counted: that is squinting, a bad camera
    angle, or someone asleep. `min_closed_frames`/`max_closed_frames`
    bound the dip.
    """
    closed_thr: float = 0.20
    open_thr: float = 0.25            # hysteresis, so noise near the line
                                      # does not produce phantom blinks
    min_closed_frames: int = 1
    max_closed_frames: int = 7
    blinks: int = 0
    _closed_run: int = 0
    _was_closed: bool = False
    history: list[float] = field(default_factory=list)

    def update(self, left6: np.ndarray, right6: np.ndarray) -> float:
        ear = (eye_aspect_ratio(left6) + eye_aspect_ratio(right6)) / 2.0
        self.history.append(ear)
        if ear < self.closed_thr:
            self._closed_run += 1
            self._was_closed = True
        elif ear > self.open_thr and self._was_closed:
            if self.min_closed_frames <= self._closed_run <= self.max_closed_frames:
                self.blinks += 1
            self._closed_run = 0
            self._was_closed = False
        return ear

    @property
    def eyes_open(self) -> bool:
        """Attention check: stops 'held up to a sleeping user'."""
        if not self.history:
            return False
        tail = self.history[-5:]
        return float(np.mean(tail)) > self.open_thr

    @property
    def seen(self) -> bool:
        return self.blinks > 0
