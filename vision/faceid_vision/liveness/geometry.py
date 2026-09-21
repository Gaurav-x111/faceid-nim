"""Planar-geometry test (parallax).

Every point on a flat photo lies on one plane, so landmark motion
between two frames is explained exactly by a single homography. A real
head has depth -- nose, ears and cheeks sit at different distances --
so a homography fits worse once the head actually turns.

Two honest caveats, both enforced below:
  * Meaningless without real pose change. Under ~10-15 deg of rotation
    everything looks planar, so we report UNKNOWN rather than "fake".
  * A curved photo or a 3D mask still passes. This is a confirm cue,
    not proof.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import cv2
import numpy as np


class Planarity(str, Enum):
    UNKNOWN = "unknown"      # not enough pose change to judge
    FLAT = "flat"            # consistent with a photo
    THREE_D = "3d"           # consistent with a real face


def planarity_residual(lm_a: np.ndarray, lm_b: np.ndarray,
                       eye_l: int, eye_r: int) -> float:
    """Mean reprojection error of a fitted homography, in eye-widths."""
    a = np.asarray(lm_a, dtype=np.float32).reshape(-1, 1, 2)
    b = np.asarray(lm_b, dtype=np.float32).reshape(-1, 2)
    H, _ = cv2.findHomography(a, b, method=0)
    if H is None:
        return float("nan")
    proj = cv2.perspectiveTransform(a, H).reshape(-1, 2)
    err = float(np.linalg.norm(proj - b, axis=1).mean())
    iod = float(np.linalg.norm(b[eye_r] - b[eye_l])) + 1e-6
    return err / iod


@dataclass
class PlanarityTracker:
    """Keeps a reference frame and compares later frames against it.

    `min_pose_delta` is measured with the same cheap yaw proxy the
    quality gate uses, so the two agree about what "turned" means.
    """
    flat_below: float = 0.012          # residual/IOD -- MEASURE THIS, do not trust it
    solid_above: float = 0.030
    min_pose_delta: float = 0.10
    eye_l: int = 0
    eye_r: int = 1
    _ref: np.ndarray | None = None
    _ref_pose: float = 0.0
    residuals: list[float] = field(default_factory=list)
    verdict: Planarity = Planarity.UNKNOWN

    def update(self, landmarks: np.ndarray, pose_proxy: float) -> Planarity:
        if self._ref is None:
            self._ref, self._ref_pose = np.asarray(landmarks, np.float32), pose_proxy
            return self.verdict
        if abs(pose_proxy - self._ref_pose) < self.min_pose_delta:
            return self.verdict                       # stay UNKNOWN, do not guess
        r = planarity_residual(self._ref, landmarks, self.eye_l, self.eye_r)
        if not np.isfinite(r):
            return self.verdict
        self.residuals.append(r)
        best = max(self.residuals)
        if best >= self.solid_above:
            self.verdict = Planarity.THREE_D
        elif best <= self.flat_below:
            self.verdict = Planarity.FLAT
        else:
            self.verdict = Planarity.UNKNOWN
        return self.verdict

    @property
    def confirms(self) -> bool:
        return self.verdict is Planarity.THREE_D

    @property
    def denies(self) -> bool:
        return self.verdict is Planarity.FLAT
