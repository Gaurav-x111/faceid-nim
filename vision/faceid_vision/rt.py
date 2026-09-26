"""One place that builds an onnxruntime session.

There used to be two, and they disagreed: the embedder pinned
``CPUExecutionProvider`` with 2 intra-op threads, the anti-spoof model
pinned the same provider with no ``sess_options`` at all (so it silently
got ORT's full default pool). Neither could ever reach a GPU, because
``Embedder.providers`` was an accepted keyword that ``build_engine`` never
passed.

Now both go through :func:`session`, which asks :mod:`hardware` what this
machine can really do and hands ORT exactly that.
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import hardware

log = logging.getLogger("faceid.vision.rt")


def session(model_path: Path | str, providers: list[str] | None = None,
            hw: hardware.HardwareProfile | None = None) -> "object":
    """Build an InferenceSession sized and provisioned for this machine.

    `providers` still wins when given (tests, and anyone who knows better
    than the probe). `hw` lets a caller reuse a profile it already
    probed instead of re-probing per model.
    """
    import onnxruntime as ort  # imported lazily so tests can run without it

    profile = hw if hw is not None else hardware.probe(Path(model_path))

    so = ort.SessionOptions()
    so.intra_op_num_threads = max(1, profile.intra_op_threads)
    so.inter_op_num_threads = max(1, profile.inter_op_threads)
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

    chosen = list(providers) if providers else list(profile.providers)
    if not chosen:
        chosen = [hardware.CPU_EXECUTION_PROVIDER]

    try:
        sess = ort.InferenceSession(
            str(model_path), sess_options=so, providers=chosen)
    except Exception:
        if chosen == [hardware.CPU_EXECUTION_PROVIDER]:
            raise
        # An accelerator that cannot be initialised must cost us speed,
        # never the feature. Fall back and say so.
        log.warning("session for %s failed with %s; retrying on CPU",
                    Path(model_path).name, ",".join(chosen))
        sess = ort.InferenceSession(
            str(model_path), sess_options=so,
            providers=[hardware.CPU_EXECUTION_PROVIDER])

    got = list(sess.get_providers())
    if got and got[0] != chosen[0] and \
            chosen[0] != hardware.CPU_EXECUTION_PROVIDER:
        # ORT downgrades to CPU with only a log line when a provider is
        # listed but its driver is unusable. Say it out loud, once, at
        # startup, instead of leaving a status line that lies.
        log.warning("%s: asked for %s, running on %s",
                    Path(model_path).name, chosen[0], got[0])
    return sess


def profile_for(model_path: Path | str) -> hardware.HardwareProfile:
    """The profile `session()` would use for this model."""
    return hardware.probe(Path(model_path))
