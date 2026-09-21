"""Face detection: YuNet (OpenCV Zoo) producing a box + 5 landmarks."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

# Landmark order produced by YuNet, and expected everywhere downstream.
LM_LEFT_EYE, LM_RIGHT_EYE, LM_NOSE, LM_MOUTH_L, LM_MOUTH_R = range(5)


@dataclass
class Face:
    box: tuple[int, int, int, int]     # x, y, w, h
    landmarks: np.ndarray              # (5, 2) float32, image pixels
    score: float

    @property
    def center(self) -> tuple[float, float]:
        x, y, w, h = self.box
        return (x + w / 2.0, y + h / 2.0)

    @property
    def inter_ocular(self) -> float:
        return float(np.linalg.norm(
            self.landmarks[LM_RIGHT_EYE] - self.landmarks[LM_LEFT_EYE]))


class Detector:
    """Thin wrapper over cv2.FaceDetectorYN."""

    def __init__(self, model_path: Path | str, score_threshold: float = 0.7,
                 nms_threshold: float = 0.3, top_k: int = 50):
        self._det = cv2.FaceDetectorYN.create(
            model=str(model_path),
            config="",
            input_size=(320, 320),
            score_threshold=score_threshold,
            nms_threshold=nms_threshold,
            top_k=top_k,
        )
        self._size: tuple[int, int] | None = None

    def detect(self, frame_bgr: np.ndarray) -> list[Face]:
        h, w = frame_bgr.shape[:2]
        if self._size != (w, h):
            self._det.setInputSize((w, h))
            self._size = (w, h)
        _, raw = self._det.detect(frame_bgr)
        if raw is None:
            return []
        faces: list[Face] = []
        for row in raw:
            x, y, bw, bh = (int(round(v)) for v in row[0:4])
            lm = np.array(row[4:14], dtype=np.float32).reshape(5, 2)
            faces.append(Face(box=(x, y, bw, bh), landmarks=lm, score=float(row[14])))
        return faces

    def largest(self, frame_bgr: np.ndarray) -> Face | None:
        """The closest face wins. Multiple faces is itself a signal worth
        logging: someone standing behind you is a shoulder-surfing case."""
        faces = self.detect(frame_bgr)
        if not faces:
            return None
        return max(faces, key=lambda f: f.box[2] * f.box[3])
