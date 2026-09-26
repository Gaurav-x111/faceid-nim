"""Dense landmarks, used only by liveness.

Optional by design. If no backend can run, the worker still works:
recognition is unaffected, and the cues that need dense points (blink,
planarity) report "unknown" instead of guessing.

Why a registry and not one import
---------------------------------
MediaPipe's packaging changed underneath us. The wheel the daemon
actually installs (0.10.x) ships the modern **Tasks** API and no longer
carries ``mp.solutions.face_mesh`` at all, so a bare
``mp.solutions.face_mesh.FaceMesh(...)`` raises ``AttributeError`` -- and
an ``except Exception`` around it turns that into a silent
``available = False``. That is exactly how blink, parallax,
challenge-response and the attention check became inert no-ops in a
shipped build while every test still passed.

So backends are tried in order, and the *reason* each one failed is kept
and reported instead of being swallowed. ``faceid-nim status`` and the
worker's ``capabilities`` reply both surface it, so "not installed" can
never again look like "working".

All backends must produce the same canonical MediaPipe Face Mesh index
ordering (1 nose tip, 10 forehead, 33/263 eye corners, 152 chin, and the
six-point eye rings in blink.py). That contract is what lets
``blink.py``, ``geometry.py`` and ``challenge.py`` stay untouched.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .liveness.blink import MP_LEFT_EYE, MP_RIGHT_EYE

log = logging.getLogger("faceid.vision.landmarks")

MP_NOSE_TIP = 1
MP_CHIN = 152
MP_FOREHEAD = 10
MP_EYE_L_OUTER = 33
MP_EYE_R_OUTER = 263

# The canonical Face Mesh topology every backend must agree on. If a
# backend ever returns a different count the indices above stop meaning
# what blink.py thinks they mean, so it is treated as a failed backend
# rather than silently wrong EARs.
CANONICAL_POINT_COUNT = 478

#: Ordered backend preference. Tasks first: it is the supported path and
#: the only one present in current wheels.
BACKEND_ORDER = ("mediapipe_tasks", "mediapipe_solutions")


@dataclass
class DenseFace:
    points: np.ndarray        # (N, 2) float32 in image pixels
    left_eye6: np.ndarray
    right_eye6: np.ndarray
    yaw_signed: float
    pitch: float


class _Backend:
    """One landmark source. Subclasses implement `detect`."""

    name = "none"

    def __init__(self) -> None:
        self.reason = ""

    @property
    def available(self) -> bool:
        return not self.reason

    def detect(self, frame_rgb: np.ndarray) -> np.ndarray | None:
        """Return (N, 2) float32 pixel landmarks, or None for no face."""
        raise NotImplementedError

    def close(self) -> None:
        pass


def _to_dense(pts: np.ndarray) -> DenseFace | None:
    """Shared conversion from canonical pixel landmarks to DenseFace.

    Returns None when the topology is not the one the rest of the liveness
    code is written against, so a backend that changes its output size
    degrades to "no landmarks" instead of producing garbage EARs.
    """
    if pts is None or pts.ndim != 2 or pts.shape[0] < CANONICAL_POINT_COUNT:
        return None
    return DenseFace(
        points=pts,
        left_eye6=pts[list(MP_LEFT_EYE)],
        right_eye6=pts[list(MP_RIGHT_EYE)],
        yaw_signed=_yaw_signed(pts),
        pitch=_pitch(pts),
    )


def _as_rgb(frame_bgr: np.ndarray) -> np.ndarray:
    """Contiguous uint8 RGB. IR cameras deliver single-channel GREY."""
    import cv2

    if frame_bgr.ndim == 2:
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_GRAY2RGB)
    else:
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    return np.ascontiguousarray(rgb)


class _TasksBackend(_Backend):
    """MediaPipe Tasks API -- `mediapipe.tasks.python.vision.FaceLandmarker`.

    Needs a `face_landmarker.task` model bundle on disk; see the
    `[model.landmarks]` entry in the manifest.
    """

    name = "mediapipe_tasks"

    def __init__(self, model_path: Path, max_faces: int = 1) -> None:
        super().__init__()
        self._task = None
        self._t0 = time.monotonic()
        self._last_ts = -1
        try:
            from mediapipe.tasks.python import BaseOptions, vision
            from mediapipe.tasks.python.vision.core.vision_task_running_mode import (
                VisionTaskRunningMode,
            )
        except Exception as e:
            self.reason = f"mediapipe Tasks API unavailable: {type(e).__name__}: {e}"
            return
        if not model_path.exists():
            self.reason = f"landmark model missing: {model_path}"
            return
        try:
            # VIDEO mode, not LIVE_STREAM: it is the right mode for
            # timestamped frames from a camera and -- unlike LIVE_STREAM
            # -- needs no user-defined result callback.
            self._task = vision.FaceLandmarker.create_from_options(
                vision.FaceLandmarkerOptions(
                    base_options=BaseOptions(model_asset_path=str(model_path)),
                    running_mode=VisionTaskRunningMode.VIDEO,
                    num_faces=max_faces,
                    output_face_blendshapes=False,
                    output_facial_transformation_matrixes=False,
                    min_face_detection_confidence=0.5,
                    min_face_presence_confidence=0.5,
                    min_tracking_confidence=0.5,
                )
            )
        except Exception as e:
            self.reason = (f"FaceLandmarker could not be created from "
                           f"{model_path.name}: {type(e).__name__}: {e}")

    def detect(self, frame_rgb: np.ndarray) -> np.ndarray | None:
        if self._task is None:
            return None
        import mediapipe as mp

        h, w = frame_rgb.shape[:2]
        # VIDEO mode requires strictly increasing timestamps in ms.
        ts = int((time.monotonic() - self._t0) * 1000)
        if ts <= self._last_ts:
            ts = self._last_ts + 1
        self._last_ts = ts
        try:
            res = self._task.detect_for_video(
                mp.Image(image_format=mp.ImageFormat.SRGB, data=frame_rgb), ts)
        except Exception as e:
            # One bad frame must not kill the worker; report and retry
            # on the next frame.
            log.debug("FaceLandmarker.detect_for_video failed: %s", e)
            return None
        faces = getattr(res, "face_landmarks", None)
        if not faces:
            return None
        return np.array([[p.x * w, p.y * h] for p in faces[0]],
                        dtype=np.float32)

    def close(self) -> None:
        if self._task is not None:
            try:
                self._task.close()
            except Exception:
                pass
            self._task = None

    def __del__(self) -> None:
        # mediapipe's own finalizer raises during interpreter teardown if
        # the task was never closed, printing a traceback on stderr. That
        # is noise in `--oneshot` and test runs, so close while the
        # module state is still alive.
        try:
            self.close()
        except Exception:
            pass


class _LegacyBackend(_Backend):
    """MediaPipe legacy `mp.solutions.face_mesh`, for older wheels only.

    Kept because a 0.9.x wheel is a legitimate install on an older
    distro. On any current wheel the import itself fails, and the reason
    it reports is what makes that visible instead of silent.
    """

    name = "mediapipe_solutions"

    def __init__(self, max_faces: int = 1) -> None:
        super().__init__()
        self._mesh = None
        try:
            import mediapipe as mp

            if not hasattr(mp, "solutions"):
                raise AttributeError(
                    "this mediapipe wheel has no 'solutions' namespace; "
                    "it ships the Tasks API only")
            self._mesh = mp.solutions.face_mesh.FaceMesh(
                static_image_mode=False,
                max_num_faces=max_faces,
                refine_landmarks=True,
                min_detection_confidence=0.5,
                min_tracking_confidence=0.5,
            )
        except Exception as e:
            self.reason = f"{type(e).__name__}: {e}"

    def detect(self, frame_rgb: np.ndarray) -> np.ndarray | None:
        if self._mesh is None:
            return None
        h, w = frame_rgb.shape[:2]
        try:
            res = self._mesh.process(frame_rgb)
        except Exception as e:
            log.debug("legacy FaceMesh.process failed: %s", e)
            return None
        if not getattr(res, "multi_face_landmarks", None):
            return None
        lm = res.multi_face_landmarks[0].landmark
        return np.array([[p.x * w, p.y * h] for p in lm], dtype=np.float32)

    def close(self) -> None:
        if self._mesh is not None:
            try:
                self._mesh.close()
            except Exception:
                pass
            self._mesh = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class FaceMesh:
    """Tries each backend in order and keeps the first that works.

    `available` is True only when a backend is live. `backend` names it
    and `reason` explains, in one line, why not when it is False.
    """

    def __init__(self, max_faces: int = 1, model_path: Path | None = None):
        self.available = False
        self.backend = "none"
        self.reason = ""
        self._active: _Backend | None = None
        self._tried: list[_Backend] = []

        if model_path is None:
            model_path = self._default_model_path()
        if not model_path.exists():
            self.reason = (f"landmark model not installed ({model_path}); "
                           "run 'sudo faceid-nim fetch-models'")
            return

        for name in BACKEND_ORDER:
            backend: _Backend
            if name == "mediapipe_tasks":
                backend = _TasksBackend(model_path, max_faces=max_faces)
            else:
                backend = _LegacyBackend(max_faces=max_faces)
            self._tried.append(backend)
            if backend.available:
                self._active = backend
                self.available = True
                self.backend = backend.name
                self.reason = ""
                log.info("dense landmarks: using %s", backend.name)
                return
            log.info("dense landmarks: %s unavailable (%s)",
                     backend.name, backend.reason)

        self.reason = "; ".join(f"{b.name}: {b.reason}" for b in self._tried)
        log.warning("dense landmarks unavailable -- %s", self.reason)

    @staticmethod
    def _default_model_path() -> Path:
        """Resolve `face_landmarker.task` from the standard locations."""
        try:
            from .models import resolve

            path, _spec = resolve("landmarks")
            return path
        except Exception:
            pass
        from .models import DEFAULT_MANIFEST, DEFAULT_MODEL_DIR

        name = "face_landmarker.task"
        if DEFAULT_MANIFEST.exists():
            try:
                from .models import load_manifest

                spec = load_manifest(DEFAULT_MANIFEST).get("landmarks")
                if spec is not None and spec.file:
                    name = spec.file
            except Exception:
                pass
        return DEFAULT_MODEL_DIR / name

    def __call__(self, frame_bgr: np.ndarray) -> DenseFace | None:
        if not self.available or self._active is None:
            return None
        try:
            pts = self._active.detect(_as_rgb(frame_bgr))
        except Exception as e:
            log.debug("landmark backend %s failed: %s", self.backend, e)
            return None
        return _to_dense(pts)

    def close(self) -> None:
        for b in (self._active, *self._tried):
            b.close()
        self._active = None


def _yaw_signed(pts: np.ndarray) -> float:
    """Signed horizontal offset of the nose between the eye corners,
    normalised by their distance. Negative = turned image-left."""
    l, r = pts[MP_EYE_L_OUTER], pts[MP_EYE_R_OUTER]
    mid = (l + r) / 2.0
    span = float(np.linalg.norm(r - l)) + 1e-6
    return float((pts[MP_NOSE_TIP][0] - mid[0]) / span)


def _pitch(pts: np.ndarray) -> float:
    """Nose height relative to the forehead-chin span. Negative = up."""
    top, bottom = pts[MP_FOREHEAD], pts[MP_CHIN]
    span = float(np.linalg.norm(bottom - top)) + 1e-6
    mid_y = (top[1] + bottom[1]) / 2.0
    return float((pts[MP_NOSE_TIP][1] - mid_y) / span)
