"""Cosine matching and k-of-n voting.

This module lives in the vision tree only so it can be unit-tested and
reused by eval/. In production the *daemon* does the matching, because
the worker must never hold templates.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np


def cosine_max(query: np.ndarray, templates: np.ndarray) -> float:
    """Best similarity against any enrolled template.

    Both sides are L2-normalised, so cosine similarity is a dot product.
    """
    if templates.size == 0:
        return -1.0
    q = np.asarray(query, dtype=np.float32).reshape(-1)
    T = np.asarray(templates, dtype=np.float32).reshape(-1, q.shape[0])
    return float((T @ q).max())


@dataclass
class Voter:
    """At least `k` of the last `n` usable frames must pass `tau`.

    One lucky frame is not evidence. This also stops the UI flickering
    between states on borderline lighting.
    """
    k: int = 3
    n: int = 5
    tau: float = 0.38
    window: deque = field(default_factory=deque)

    def __post_init__(self) -> None:
        self.window = deque(maxlen=self.n)

    def push(self, similarity: float) -> None:
        self.window.append(float(similarity))

    @property
    def passes(self) -> int:
        return sum(1 for s in self.window if s >= self.tau)

    @property
    def decided(self) -> bool:
        return self.passes >= self.k

    @property
    def best(self) -> float:
        return max(self.window) if self.window else -1.0

    def progress(self) -> float:
        return min(1.0, self.passes / float(self.k)) if self.k else 0.0
