"""Dense landmarks (MediaPipe Face Mesh), used only by liveness.

Optional by design. If MediaPipe is unavailable the worker still runs:
recognition is unaffected, and the cues that need dense points
(blink, planarity) simply report "unknown" instead of guessing.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .liveness.blink import MP_LEFT_EYE, MP_RIGHT_EYE

MP_NOSE_TIP = 1
MP_CHIN = 152
MP_FOREHEAD = 10
MP_EYE_L_OUTER = 33
MP_EYE_R_OUTER = 263


@dataclass
class DenseFace:
    points: np.ndarray        # (N, 2) float32 in image pixels
    left_eye6: np.ndarray
    right_eye6: np.ndarray
    yaw_signed: float
    pitch: float


class FaceMesh:
    def __init__(self, max_faces: int = 1):
        self.available = False
        self._mesh = None
        try:
            import mediapipe as mp
            self._mesh = mp.solutions.face_mesh.FaceMesh(
                static_image_mode=False,
                max_num_faces=max_faces,
                refine_landmarks=True,
                min_detection_confidence=0.5,
                min_tracking_confidence=0.5,
            )
            self.available = True
        except Exception:
            self.available = False

    def __call__(self, frame_bgr: np.ndarray) -> DenseFace | None:
        if not self.available or self._mesh is None:
            return None
        import cv2
        h, w = frame_bgr.shape[:2]
        # MediaPipe Face Mesh wants RGB. IR cameras can deliver a
        # single-channel GREY frame; broadcast it instead of crashing
        # on COLOR_BGR2RGB of a 2D array.
        if frame_bgr.ndim == 2:
            rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_GRAY2RGB)
        else:
            rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        res = self._mesh.process(rgb)
        if not res.multi_face_landmarks:
            return None
        lm = res.multi_face_landmarks[0].landmark
        pts = np.array([[p.x * w, p.y * h] for p in lm], dtype=np.float32)
        return DenseFace(
            points=pts,
            left_eye6=pts[list(MP_LEFT_EYE)],
            right_eye6=pts[list(MP_RIGHT_EYE)],
            yaw_signed=_yaw_signed(pts),
            pitch=_pitch(pts),
        )

    def close(self) -> None:
        if self._mesh is not None:
            self._mesh.close()


def _yaw_signed(pts: np.ndarray) -> float:
    """Signed horizontal offset of the nose between the eye corners,
    normalised by their distance. Negative = turned image-left."""
    l, r = pts[MP_EYE_L_OUTER], pts[MP_EYE_R_OUTER]
    mid = (l + r) / 2.0
    span = float(np.linalg.norm(r - l)) + 1e-6
    return float((pts[MP_NOSE_TIP][0] - mid[0]) / span)


def _pitch(pts: np.ndarray) -> float:
    """Nose height relative to the forehead-chin span. Negative = up."""
    top, bottom = pts[MP_FOREHEAD], pts[MP_CHIN]
    span = float(np.linalg.norm(bottom - top)) + 1e-6
    mid_y = (top[1] + bottom[1]) / 2.0
    return float((pts[MP_NOSE_TIP][1] - mid_y) / span)
