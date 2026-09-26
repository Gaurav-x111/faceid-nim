"""Passive anti-spoof CNN (MiniFASNet-style ONNX).

Treated as one vote among several, never as the authority. These
models lose a lot of accuracy on cameras and lighting they were not
trained on, and a project that trusts one blindly has effectively
outsourced its security to someone else's training set.

If the model is missing the detector degrades to "no opinion" rather
than failing the scan.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np


class PassiveAntiSpoof:
    def __init__(self, model_path: Path | str | None, input_size: int = 80,
                 scale: float = 2.7, deny_threshold: float = 0.7,
                 providers: list[str] | None = None):
        self.available = False
        #: Why this model is not running, in one line. Empty when it is.
        #: This exists because the deny cue silently never fired for the
        #: whole life of the feature while the UI still implied a check
        #: was running.
        self.reason = ""
        self.model_path = str(model_path) if model_path else ""
        self.deny_threshold = deny_threshold
        self.input_size = input_size
        self.scale = scale
        self._sess = None
        self._input = ""
        if model_path is None:
            self.reason = ("no anti-spoof model installed; the "
                           "antispoof_model deny cue cannot fire")
            return
        try:
            # Same factory as the recognizer, so the two cannot drift
            # apart on provider choice or thread sizing again.
            from .. import rt

            self._sess = rt.session(model_path, providers=providers)
            self._input = self._sess.get_inputs()[0].name
            self.available = True
        except Exception as e:
            self.available = False
            self.reason = (f"anti-spoof model {Path(model_path).name} could "
                           f"not be loaded: {type(e).__name__}: {e}")

    @property
    def providers(self) -> list[str]:
        return list(self._sess.get_providers()) if self._sess is not None else []

    def _crop(self, frame_bgr: np.ndarray, box) -> np.ndarray:
        """MiniFASNet wants context around the face, not a tight crop:
        the background is part of what gives a screen away."""
        x, y, w, h = box
        cx, cy = x + w / 2.0, y + h / 2.0
        side = max(w, h) * self.scale
        H, W = frame_bgr.shape[:2]
        x0, y0 = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
        x1, y1 = int(min(W, cx + side / 2)), int(min(H, cy + side / 2))
        patch = frame_bgr[y0:y1, x0:x1]
        if patch.size == 0:
            patch = frame_bgr
        if patch.ndim == 2:
            # The CNN input is (1, 3, 80, 80); a single-channel crop
            # would transpose to a 1-channel blob and fail its session.
            patch = cv2.cvtColor(patch, cv2.COLOR_GRAY2BGR)
        return cv2.resize(patch, (self.input_size, self.input_size))

    def spoof_probability(self, frame_bgr: np.ndarray, box) -> float | None:
        """Probability the face is a presentation attack, or None if we
        have no model loaded."""
        if not self.available or self._sess is None:
            return None
        patch = self._crop(frame_bgr, box).astype(np.float32) / 255.0
        blob = np.transpose(patch, (2, 0, 1))[None]
        try:
            out = self._sess.run(None, {self._input: blob})[0]
        except Exception:
            return None
        arr = np.asarray(out, dtype=np.float64).reshape(-1)
        if arr.size == 0:
            return None
        if arr.size == 1:
            # Single-logit model: sigmoid = P(spoof).
            p_spoof = float(1.0 / (1.0 + np.exp(-arr[0])))
            return p_spoof
        e = np.exp(arr - arr.max())
        probs = e / e.sum()
        # MiniFASNet convention: class 1 is the live class, 0 and 2 are
        # print and replay attacks. Verify against your model card.
        live = float(probs[1]) if probs.shape[0] >= 3 else float(probs[-1])
        return 1.0 - live

    def denies(self, frame_bgr: np.ndarray, box) -> bool:
        p = self.spoof_probability(frame_bgr, box)
        return p is not None and p > self.deny_threshold
