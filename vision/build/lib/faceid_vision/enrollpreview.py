"""Enrollment preview: small JPEG thumbnails for the enrolling app.

This is the one place in the worker that turns a frame into something
that leaves the process, so the constraints are all here:

  * Downscaled to ~320px on the long edge and JPEG quality ~70, which
    lands well under 20 KB. Base64 inflates that by a third, so a
    preview line stays under the 64 KB the daemon's socket reader pulls
    per recv -- no reframing needed on either side.
  * Nothing is written to disk. The encoded bytes exist in memory for
    the length of one yield.
  * Only reached when the daemon explicitly asks for a preview, which
    it only does for an enrollment session belonging to the caller.
    A normal unlock scan never produces one.

Pose guidance is computed here too, so the app's overlay and the
worker's quality gate agree about what "turned far enough" means
instead of each having its own opinion.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Iterator

import cv2
import numpy as np

from .detect import Face
from .quality import QualityConfig, QualityReport, assess

PREVIEW_LONG_EDGE = 320
JPEG_QUALITY = 70
# Hard ceiling. If an encode somehow exceeds it we drop the frame
# rather than risk a line the daemon has to reassemble.
MAX_JPEG_BYTES = 20 * 1024

# Status strings, matching daemon/src/enroll.rs exactly.
ST_ACQUIRING = "acquiring"
ST_GOOD = "good"
ST_POSE_COMPLETE = "pose_complete"
ST_FAILED = "failed"


@dataclass
class PreviewConfig:
    long_edge: int = PREVIEW_LONG_EDGE
    quality: int = JPEG_QUALITY
    # One preview every N usable frames. 1 gives ~30 fps on a 30 fps
    # camera (the camera's own rate is the ceiling); 2 halves that to
    # 10-15 fps, which reads as choppy during enrollment.
    every: int = 1
    mirror: bool = True          # users expect a mirror, not a camera view
    draw_guides: bool = True


@dataclass
class PreviewState:
    """Per-scan counters. One instance per pose capture."""
    frames: int = 0
    emitted: int = 0
    last_status: str = ST_ACQUIRING
    good_frames: int = 0
    reasons: list[str] = field(default_factory=list)


def thumbnail(frame_bgr: np.ndarray, cfg: PreviewConfig) -> np.ndarray:
    h, w = frame_bgr.shape[:2]
    scale = cfg.long_edge / float(max(h, w))
    if scale < 1.0:
        frame_bgr = cv2.resize(frame_bgr, (int(w * scale), int(h * scale)),
                               interpolation=cv2.INTER_AREA)
    if cfg.mirror:
        frame_bgr = cv2.flip(frame_bgr, 1)
    return frame_bgr


def _overlay(img: np.ndarray, face: Face | None, ok: bool,
             src_shape: tuple[int, int], cfg: PreviewConfig) -> np.ndarray:
    """Draw the detection box so the user can see they are framed.

    Purely cosmetic: the box is derived from the detection the quality
    gate already ran, never from anything the app sends back.
    """
    if img.ndim == 2:
        # Rectangle drawing needs per-channel colours, and encoding the
        # overlay as a colour JPEG is what the app's player expects.
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    if face is None or not cfg.draw_guides:
        return img
    sh, sw = src_shape
    ih, iw = img.shape[:2]
    sx, sy = iw / float(sw), ih / float(sh)
    x, y, w, h = face.box
    x0, y0 = int(x * sx), int(y * sy)
    x1, y1 = int((x + w) * sx), int((y + h) * sy)
    if cfg.mirror:
        x0, x1 = iw - x1, iw - x0
    color = (120, 220, 130) if ok else (150, 150, 150)
    cv2.rectangle(img, (x0, y0), (x1, y1), color, 2)
    return img


def encode_jpeg(img: np.ndarray, quality: int) -> bytes | None:
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        return None
    data = buf.tobytes()
    if len(data) > MAX_JPEG_BYTES:
        # Re-encode once at a lower quality before giving up.
        ok, buf = cv2.imencode(".jpg", img,
                               [int(cv2.IMWRITE_JPEG_QUALITY), max(35, quality - 25)])
        if not ok:
            return None
        data = buf.tobytes()
        if len(data) > MAX_JPEG_BYTES:
            return None
    return data


def status_for(quality: QualityReport | None, face: Face | None,
               state: PreviewState) -> str:
    """Collapse the quality report into one of the four app statuses."""
    if face is None:
        return ST_ACQUIRING
    if quality is None or not quality.ok:
        return ST_ACQUIRING
    return ST_GOOD


def preview_events(frame_bgr: np.ndarray,
                   face: Face | None,
                   quality: QualityReport | None,
                   state: PreviewState,
                   cfg: PreviewConfig = PreviewConfig()) -> Iterator[dict]:
    """Yield the events the worker should emit for this frame.

    Two kinds:
        {"ev": "preview",     "jpeg": "<base64>"}
        {"ev": "pose_status", "status": "...", "reason": "..."}

    `pose_status` is only emitted when the status actually changes, so
    a steady scan produces one status line, not thirty.
    """
    state.frames += 1

    status = status_for(quality, face, state)
    if status == ST_GOOD:
        state.good_frames += 1
    elif quality is not None and quality.reason:
        if quality.reason not in state.reasons:
            state.reasons.append(quality.reason)

    if status != state.last_status:
        state.last_status = status
        yield {
            "ev": "pose_status",
            "status": status,
            "reason": "" if status == ST_GOOD else (
                quality.reason if quality is not None else "no face"),
        }

    if state.frames % max(1, cfg.every) != 0:
        return

    img = thumbnail(frame_bgr, cfg)
    img = _overlay(img, face, status == ST_GOOD, frame_bgr.shape[:2], cfg)
    data = encode_jpeg(img, cfg.quality)
    if data is None:
        return
    state.emitted += 1
    yield {"ev": "preview", "jpeg": base64.b64encode(data).decode("ascii")}


def assess_for_preview(frame_bgr: np.ndarray, face: Face | None,
                       qcfg: QualityConfig) -> QualityReport | None:
    """Convenience wrapper so callers do not have to guard `face`."""
    if face is None:
        return None
    return assess(frame_bgr, face, qcfg)
