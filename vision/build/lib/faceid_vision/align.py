"""Similarity-transform alignment onto the ArcFace 112x112 template."""
from __future__ import annotations

import cv2
import numpy as np

ARC_REF = np.array(
    [[38.2946, 51.6963],
     [73.5318, 51.5014],
     [56.0252, 71.7366],
     [41.5493, 92.3655],
     [70.7299, 92.2041]], dtype=np.float32)


def align(frame_bgr: np.ndarray, lm5: np.ndarray,
          size: tuple[int, int] = (112, 112)) -> np.ndarray:
    """Warp the face onto the reference template.

    lm5: (5, 2) float32 in image pixels, ordered
    [left eye, right eye, nose, left mouth, right mouth].
    """
    lm5 = np.asarray(lm5, dtype=np.float32).reshape(5, 2)
    ref = ARC_REF
    if size != (112, 112):
        ref = ARC_REF * np.array([size[0] / 112.0, size[1] / 112.0], dtype=np.float32)
    M, _ = cv2.estimateAffinePartial2D(lm5, ref, method=cv2.LMEDS)
    if M is None:
        raise ValueError("could not estimate alignment transform")
    return cv2.warpAffine(frame_bgr, M, size, flags=cv2.INTER_LINEAR, borderValue=0)
