"""Infrared cues.

Screens emit almost no infrared, so a face on a phone or monitor comes
out dark or structureless in an IR stream. Skin reflects IR in a way
prints do not. This is the single strongest cheap cue available, and
it is the reason IR laptops are meaningfully harder to spoof than
RGB-only ones.

Most IR cameras need their emitter enabled with a vendor-specific UVC
command; see the linux-enable-ir-emitter project. Capability must be
detected at runtime -- never assume IR exists.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class IRReport:
    available: bool = False
    screen_dark: bool = False        # deny cue
    skin_response_ok: bool = False   # confirm cue
    mean_lum: float = 0.0
    contrast: float = 0.0


def analyse_ir_face(ir_frame: np.ndarray, box,
                    dark_lum: float = 35.0,
                    min_contrast: float = 18.0) -> IRReport:
    x, y, w, h = box
    H, W = ir_frame.shape[:2]
    crop = ir_frame[max(0, y):min(H, y + h), max(0, x):min(W, x + w)]
    if crop.size == 0:
        return IRReport(available=True)
    gray = crop if crop.ndim == 2 else cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    lum = float(gray.mean())
    contrast = float(gray.std())
    return IRReport(
        available=True,
        screen_dark=lum < dark_lum or contrast < min_contrast,
        skin_response_ok=lum >= dark_lum and contrast >= min_contrast,
        mean_lum=lum,
        contrast=contrast,
    )


def emitter_difference(on_frame: np.ndarray, off_frame: np.ndarray,
                       box, min_delta: float = 12.0) -> bool:
    """Best-case IR test: a real face lights up when the emitter turns
    on. A screen barely changes."""
    x, y, w, h = box
    def crop(f):
        H, W = f.shape[:2]
        c = f[max(0, y):min(H, y + h), max(0, x):min(W, x + w)]
        return c if c.ndim == 2 else cv2.cvtColor(c, cv2.COLOR_BGR2GRAY)
    a, b = crop(on_frame).astype(np.float32), crop(off_frame).astype(np.float32)
    if a.shape != b.shape or a.size == 0:
        return False
    return float((a - b).mean()) >= min_delta
