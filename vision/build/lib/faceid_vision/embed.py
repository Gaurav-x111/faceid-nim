"""Embedding network: aligned 112x112 crop -> L2-normalised vector.

Preprocessing constants are model-specific. The defaults here match
ArcFace/SFace-style ONNX exports ((x - 127.5) / 127.5, RGB). If you
swap in a model with a different model card, change `scale`, `mean`
and `swap_rb` -- and change `model_id`, because embeddings from
different models are not comparable and every user must re-enroll.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np


class Embedder:
    def __init__(self, model_path: Path | str, model_id: str,
                 scale: float = 1 / 127.5,
                 mean: tuple[float, float, float] = (127.5, 127.5, 127.5),
                 swap_rb: bool = True,
                 providers: list[str] | None = None):
        import onnxruntime as ort  # imported lazily so tests can run without it

        so = ort.SessionOptions()
        so.intra_op_num_threads = 2          # a scan is latency-bound, not throughput-bound
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.sess = ort.InferenceSession(
            str(model_path), sess_options=so,
            providers=providers or ["CPUExecutionProvider"])
        self.input_name = self.sess.get_inputs()[0].name
        shape = self.sess.get_inputs()[0].shape
        self.size = (int(shape[3]), int(shape[2])) if len(shape) == 4 else (112, 112)
        self.model_id = model_id
        self.scale, self.mean, self.swap_rb = scale, mean, swap_rb

    @property
    def dim(self) -> int:
        out = self.sess.get_outputs()[0].shape
        return int(out[-1])

    def __call__(self, face_bgr: np.ndarray) -> np.ndarray:
        if face_bgr.ndim == 2:
            # The recognizer was trained on 3-channel RGB. A GREY IR
            # crop gets broadcast to BGR so blobFromImage never sees a
            # channel count the model input does not accept.
            face_bgr = cv2.cvtColor(face_bgr, cv2.COLOR_GRAY2BGR)
        blob = cv2.dnn.blobFromImage(face_bgr, self.scale, self.size,
                                     self.mean, swapRB=self.swap_rb, crop=False)
        v = self.sess.run(None, {self.input_name: blob})[0][0].astype(np.float32)
        n = float(np.linalg.norm(v))
        if n < 1e-8:
            raise ValueError("degenerate embedding")
        return v / n
