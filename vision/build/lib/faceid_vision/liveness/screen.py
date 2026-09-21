"""Deny cues for a face shown on a screen or a glossy print.

None of these is conclusive alone. They are cheap, they need no model,
and they fire on the easiest and most common attack.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from ..detect import Face


@dataclass
class ScreenCues:
    glare: bool = False
    device_frame: bool = False
    moire: bool = False
    glare_frac: float = 0.0
    moire_energy: float = 0.0

    @property
    def any_deny(self) -> list[str]:
        out = []
        if self.glare:
            out.append("glare")
        if self.device_frame:
            out.append("device_frame")
        if self.moire:
            out.append("moire")
        return out


def _glare(face_crop_bgr: np.ndarray, thr: int = 250,
           min_frac: float = 0.012) -> tuple[bool, float]:
    """Screens and gloss make small, hard-edged, blown-out blobs. Skin
    lit normally does not."""
    gray = face_crop_bgr if face_crop_bgr.ndim == 2 else \
        cv2.cvtColor(face_crop_bgr, cv2.COLOR_BGR2GRAY)
    mask = (gray >= thr).astype(np.uint8)
    if mask.sum() == 0:
        return False, 0.0
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    area = float(gray.size)
    blob = max((stats[i, cv2.CC_STAT_AREA] for i in range(1, n)), default=0)
    frac = blob / area
    return frac >= min_frac, frac


def _device_frame(frame_bgr: np.ndarray, face: Face,
                  min_ratio: float = 1.25) -> bool:
    """A phone or tablet held up shows a bezel: a large quadrilateral
    that encloses the face and is clearly bigger than it."""
    gray = frame_bgr if frame_bgr.ndim == 2 else \
        cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 50, 150)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    fx, fy, fw, fh = face.box
    face_area = float(fw * fh)
    cx, cy = face.center
    for c in contours:
        if cv2.contourArea(c) < face_area * min_ratio:
            continue
        approx = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
        if len(approx) != 4 or not cv2.isContourConvex(approx):
            continue
        if cv2.pointPolygonTest(approx, (float(cx), float(cy)), False) < 0:
            continue
        x, y, w, h = cv2.boundingRect(approx)
        if w * h > face_area * min_ratio and (x > 2 or y > 2):
            return True                      # a real wall behind you rarely
                                             # forms a convex quad around your head
    return False


def _moire(face_crop_bgr: np.ndarray, thr: float = 0.055) -> tuple[bool, float]:
    """Photographing a pixel grid makes interference patterns, which
    show up as off-centre energy in the frequency domain."""
    gray = face_crop_bgr if face_crop_bgr.ndim == 2 else \
        cv2.cvtColor(face_crop_bgr, cv2.COLOR_BGR2GRAY)
    gray = gray.astype(np.float32)
    gray = cv2.resize(gray, (128, 128))
    gray *= np.outer(np.hanning(128), np.hanning(128)).astype(np.float32)
    spec = np.abs(np.fft.fftshift(np.fft.fft2(gray)))
    total = float(spec.sum()) + 1e-6
    yy, xx = np.mgrid[0:128, 0:128]
    r = np.hypot(yy - 64, xx - 64)
    band = float(spec[(r > 24) & (r < 56)].sum()) / total
    return band >= thr, band


def screen_report(frame_bgr: np.ndarray, face: Face) -> ScreenCues:
    x, y, w, h = face.box
    H, W = frame_bgr.shape[:2]
    crop = frame_bgr[max(0, y):min(H, y + h), max(0, x):min(W, x + w)]
    if crop.size == 0:
        return ScreenCues()
    glare, gfrac = _glare(crop)
    moire, menergy = _moire(crop)
    return ScreenCues(
        glare=glare,
        device_frame=_device_frame(frame_bgr, face),
        moire=moire,
        glare_frac=gfrac,
        moire_energy=menergy,
    )
