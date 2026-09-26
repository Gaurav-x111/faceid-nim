"""Room-light metering for `auto` camera mode.

Measures mean luma (0-255) from a few RGB frames so the worker can pick
the recognition spectrum per scan: dark room -> IR primary, lit room ->
RGB primary. Hysteresis band avoids flicker at the boundary.
"""
from __future__ import annotations


AUTO_DARK_LUMA = 28.0
AUTO_LIGHT_LUMA = 42.0


def mean_luma_bgr(frame) -> float:
    """Mean luma of a BGR (or GREY) frame, 0-255."""
    try:
        import numpy as np  # noqa: PLC0415
    except ImportError:
        return 128.0
    try:
        if frame is None or getattr(frame, "size", 0) == 0:
            return 0.0
        if frame.ndim == 2:
            return float(frame.mean())
        b = frame[:, :, 0].astype("float32")
        g = frame[:, :, 1].astype("float32")
        r = frame[:, :, 2].astype("float32")
        return float((0.114 * b + 0.587 * g + 0.299 * r).mean())
    except Exception:
        return 0.0


def pick_spectrum(luma: float, last: str | None,
                  dark: float = AUTO_DARK_LUMA,
                  light: float = AUTO_LIGHT_LUMA) -> tuple[str, float]:
    """Pick "ir" or "rgb" from a luma reading with hysteresis.

    Below `dark` -> ir, above `light` -> rgb, in-band -> keep `last`
    (default rgb when no history).
    """
    if luma < dark:
        return "ir", luma
    if luma > light:
        return "rgb", luma
    return (last if last in ("ir", "rgb") else "rgb"), luma
