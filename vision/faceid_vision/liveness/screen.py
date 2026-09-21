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
    # Measured prominence score, independent of the deny threshold.
    # Kept out of the deny bit so callers can log "what we measured"
    # separately from "what we did about it".
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


# The old detector measured the *fraction* of total FFT energy inside a
# mid-frequency annulus. Ordinary webcam content -- room geometry, JPEG
# blocks, skin grain -- already parks 10-20% of its energy there, so on
# a real camera the deny threshold sat ~3x below the noise floor and
# fired on every genuine frame (measured 0.15 on a plain office scene,
# no face present). Photographing a pixel grid is not broadband: it is
# one dominant sharp spatial frequency. Measure *prominence* instead --
# the strongest component in the annulus relative to the annulus
# baseline (median). Genuine texture measures ~4-6x baseline; a screen
# or print grid measures 40-500x. See tests/test_moire.py and
# "MOIRE" in the daemon audit docs.
# A screen or print grid measures 40-500x (see tests/test_moire.py).
# Threshold is 20: a real office scene measures at most ~5.5 so there is
# ~3.6x headroom for genuine crop texture, an 8px JPEG block lattice
# (~38) is still vetoed, and the youngest real-world attack still sits
# 950x above the line. Moire is additionally a soft cue in scan.py
# (needs 3 of the last 5 usable frames), so a single bright edge or
# exposure blip cannot kill an authentication by itself.
MOIRE_THRESHOLD = 20.0


def _moire(face_crop_bgr: np.ndarray, thr: float = MOIRE_THRESHOLD) -> tuple[bool, float]:
    """One dominant periodic frequency inside the face crop.

    A face literally on a screen or glossy print leaves a sharp grid
    peak in the mid-frequency annulus of the frame's spectrum. Ordinary
    texture leaves a flat annulus. The score is the peak-to-median
    ratio of that annulus, so it is robust to absolute brightness/JPG
    load and needs no per-camera calibration.
    """
    gray = face_crop_bgr if face_crop_bgr.ndim == 2 else \
        cv2.cvtColor(face_crop_bgr, cv2.COLOR_BGR2GRAY)
    if gray.size < 16:
        return False, 0.0
    gray = gray.astype(np.float32)
    # INTER_AREA anti-aliases while shrinking, so downsampling a genuine
    # face cannot manufacture periodic texture; any grid the sensor
    # actually captured survives the scale-down.
    gray = cv2.resize(gray, (128, 128), interpolation=cv2.INTER_AREA)
    gray *= np.outer(np.hanning(128), np.hanning(128)).astype(np.float32)
    spec = np.abs(np.fft.fftshift(np.fft.fft2(gray)))
    yy, xx = np.mgrid[0:128, 0:128]
    r = np.hypot(yy - 64, xx - 64)
    annulus = spec[(r > 24) & (r < 56)]
    if annulus.size == 0:
        return False, 0.0
    med = float(np.percentile(annulus, 50))
    if med < 1e-3:
        # A flat, featureless crop has no spectral structure to judge.
        return False, 0.0
    score = float(annulus.max()) / med
    return score >= thr, score


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
