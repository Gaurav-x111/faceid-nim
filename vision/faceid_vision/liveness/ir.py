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
    x, y, w, h = (int(v) for v in box)
    H, W = ir_frame.shape[:2]
    # Rescale the RGB box into IR pixels: the two sensors often differ
    # in resolution/FOV. Caller passes the RGB frame size via box scale;
    # here we clamp defensively and leave an unknown zone (dead-band)
    # so a dim face is not a deny and a bright screen is not a confirm.
    crop = ir_frame[max(0, y):min(H, y + h), max(0, x):min(W, x + w)]
    if crop.size == 0:
        return IRReport(available=True)
    gray = crop if crop.ndim == 2 else cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    lum = float(gray.mean())
    contrast = float(gray.std())
    # Dead-band: dark+flat = screen, bright+textured = skin, else unknown.
    screen_dark = lum < (dark_lum - 10.0) or contrast < (min_contrast - 6.0)
    skin_response_ok = lum >= dark_lum and contrast >= min_contrast
    return IRReport(
        available=True,
        screen_dark=screen_dark,
        skin_response_ok=skin_response_ok if not screen_dark else False,
        mean_lum=lum,
        contrast=contrast,
    )


def emitter_difference(on_frame: np.ndarray, off_frame: np.ndarray,
                       box, min_delta: float = 12.0) -> bool:
    """Best-case IR test: a real face lights up when the emitter turns
    on. A screen barely changes.

    NOT WIRED INTO ANY SCAN. This is a helper with no caller: it needs
    two frames captured with the emitter off and then on, and the worker
    only ever sees one stream, so nothing in the unlock path can produce
    the pair this compares. `ir_emitter.py` can tell you whether the
    emitter tool is installed; it cannot run this.

    It is kept, and unit-tested, because it is the strongest cheap
    anti-spoof test available on IR hardware and wiring it would need a
    deliberate emitter-toggle sequence, not an accident. Do not report it
    as an active check.
    """
    x, y, w, h = (int(v) for v in box)
    def crop(f):
        H, W = f.shape[:2]
        c = f[max(0, y):min(H, y + h), max(0, x):min(W, x + w)]
        if c.size == 0:
            # A box outside the frame yields an empty crop, and
            # cvtColor on an empty array raises. Bail out here rather
            # than crashing a scan over a bad box.
            return None
        return c if c.ndim == 2 else cv2.cvtColor(c, cv2.COLOR_BGR2GRAY)
    a, b = crop(on_frame), crop(off_frame)
    if a is None or b is None:
        return False
    a, b = a.astype(np.float32), b.astype(np.float32)
    if a.shape != b.shape or a.size == 0:
        return False
    return float((a - b).mean()) >= min_delta
