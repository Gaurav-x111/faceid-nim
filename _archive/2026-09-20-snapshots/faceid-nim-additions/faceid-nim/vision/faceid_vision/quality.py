"""Quality gate.

Scoring a bad frame is worse than skipping it: blur and darkness push
similarity around in both directions, which corrupts both the accept
and the reject path.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .detect import Face, LM_LEFT_EYE, LM_MOUTH_L, LM_MOUTH_R, LM_NOSE, LM_RIGHT_EYE


@dataclass
class QualityConfig:
    min_box_px: int = 96              # face smaller than this: too far away
    min_blur_var: float = 45.0        # variance of Laplacian
    min_mean_lum: float = 40.0
    max_mean_lum: float = 215.0
    max_clipped_frac: float = 0.25    # fraction of pixels at 0 or 255
    max_yaw_ratio: float = 0.38       # crude yaw proxy, see below
    max_roll_deg: float = 25.0
    min_score: float = 0.8


@dataclass
class QualityReport:
    ok: bool
    reason: str = ""
    blur: float = 0.0
    luminance: float = 0.0
    yaw_ratio: float = 0.0
    roll_deg: float = 0.0


def _roll_degrees(lm: np.ndarray) -> float:
    dx, dy = lm[LM_RIGHT_EYE] - lm[LM_LEFT_EYE]
    return float(abs(np.degrees(np.arctan2(dy, dx))))


def _yaw_ratio(lm: np.ndarray) -> float:
    """Horizontal offset of the nose from the midpoint between the eyes,
    normalised by inter-ocular distance. 0 is frontal; it grows as the
    head turns. Not a calibrated angle -- a cheap monotonic proxy."""
    eye_mid = (lm[LM_LEFT_EYE] + lm[LM_RIGHT_EYE]) / 2.0
    mouth_mid = (lm[LM_MOUTH_L] + lm[LM_MOUTH_R]) / 2.0
    iod = np.linalg.norm(lm[LM_RIGHT_EYE] - lm[LM_LEFT_EYE]) + 1e-6
    axis = (eye_mid + mouth_mid) / 2.0
    return float(abs(lm[LM_NOSE][0] - axis[0]) / iod)


def assess(frame_bgr: np.ndarray, face: Face,
           cfg: QualityConfig = QualityConfig()) -> QualityReport:
    x, y, w, h = face.box
    H, W = frame_bgr.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return QualityReport(False, "box outside frame")

    crop = frame_bgr[y0:y1, x0:x1]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)

    blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    lum = float(gray.mean())
    clipped = float(((gray <= 2) | (gray >= 253)).mean())
    yaw = _yaw_ratio(face.landmarks)
    roll = _roll_degrees(face.landmarks)
    rep = QualityReport(True, "", blur, lum, yaw, roll)

    if face.score < cfg.min_score:
        rep.ok, rep.reason = False, "low detection confidence"
    elif min(w, h) < cfg.min_box_px:
        rep.ok, rep.reason = False, "face too small"
    elif blur < cfg.min_blur_var:
        rep.ok, rep.reason = False, "blurry"
    elif lum < cfg.min_mean_lum:
        rep.ok, rep.reason = False, "too dark"
    elif lum > cfg.max_mean_lum or clipped > cfg.max_clipped_frac:
        rep.ok, rep.reason = False, "overexposed"
    elif yaw > cfg.max_yaw_ratio:
        rep.ok, rep.reason = False, "head turned too far"
    elif roll > cfg.max_roll_deg:
        rep.ok, rep.reason = False, "head tilted too far"
    return rep
