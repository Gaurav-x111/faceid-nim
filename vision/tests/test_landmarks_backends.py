"""The landmark backend registry must never fail silently.

The bug this file exists to prevent: the worker asked mediapipe for
``mp.solutions.face_mesh``, an API the installed wheel no longer ships,
and a bare ``except Exception`` turned the resulting AttributeError into
``available = False``. Blink, parallax, challenge-response and the
attention check were then inert no-ops in a shipped build while every
test still passed, because the tests all injected a fake mesh.

So: backends are tried in order, the winning one is named, and a failure
carries a reason. The canonical landmark indices must also be identical
across backends, or the EAR maths in blink.py is silently meaningless.
"""
import sys

import numpy as np
import pytest

from faceid_vision import landmarks as L
from faceid_vision.liveness.blink import (MP_LEFT_EYE, MP_RIGHT_EYE,
                                          eye_aspect_ratio)


def _canonical_pts(n=L.CANONICAL_POINT_COUNT):
    """A deterministic 478-point cloud, so indices mean what they say."""
    rng = np.random.default_rng(7)
    return (rng.random((n, 2)).astype(np.float32) * 100.0 + 50.0)


class _StubLandmarker:
    """Mimics the mediapipe Tasks result shape without the dependency."""

    def __init__(self, pts):
        self._pts = pts
        self.calls = 0

    def detect_for_video(self, image, ts):
        self.calls += 1
        assert isinstance(ts, int) and ts >= 0
        h, w = image.height, image.width

        class _P:
            def __init__(self, p):
                self.x, self.y = p[0] / w, p[1] / h

        class _R:
            face_landmarks = [[_P(p) for p in self._pts]]

        return _R()

    def close(self):
        self.closed = True


# ---- the index contract blink.py/geometry.py depend on ----------------

def test_to_dense_uses_the_canonical_indices():
    pts = _canonical_pts()
    dense = L._to_dense(pts)
    assert dense is not None
    # Exactly the tuples blink.py declares, in the same order.
    assert np.array_equal(dense.left_eye6, pts[list(MP_LEFT_EYE)])
    assert np.array_equal(dense.right_eye6, pts[list(MP_RIGHT_EYE)])
    assert dense.points.shape == (L.CANONICAL_POINT_COUNT, 2)
    assert dense.points.dtype == np.float32


def test_yaw_and_pitch_come_from_the_canonical_points():
    pts = _canonical_pts()
    d = L._to_dense(pts)
    # Recomputed independently from the named indices.
    l, r = pts[L.MP_EYE_L_OUTER], pts[L.MP_EYE_R_OUTER]
    span = np.linalg.norm(r - l) + 1e-6
    expect_yaw = (pts[L.MP_NOSE_TIP][0] - (l[0] + r[0]) / 2.0) / span
    assert d.yaw_signed == pytest.approx(float(expect_yaw), rel=1e-5)
    top, bot = pts[L.MP_FOREHEAD], pts[L.MP_CHIN]
    vspan = np.linalg.norm(bot - top) + 1e-6
    expect_pitch = (pts[L.MP_NOSE_TIP][1] - (top[1] + bot[1]) / 2.0) / vspan
    assert d.pitch == pytest.approx(float(expect_pitch), rel=1e-5)


def test_wrong_topology_degrades_to_none_not_garbage():
    """A backend that changed its output size must not produce EARs from
    whatever indices happen to exist."""
    assert L._to_dense(np.zeros((400, 2), np.float32)) is None
    assert L._to_dense(np.zeros((10, 2), np.float32)) is None
    assert L._to_dense(None) is None


# ---- registry behaviour ------------------------------------------------

def test_missing_model_reports_an_actionable_reason(monkeypatch, tmp_path):
    """No model must produce a message that tells the user what to do,
    not a bare False -- and must not import mediapipe at all."""
    fm = L.FaceMesh(model_path=tmp_path / "nope.task")
    assert fm.available is False
    assert fm.backend == "none"
    assert "fetch-models" in fm.reason
    assert fm(_frame()) is None


def test_registry_tries_tasks_before_legacy(monkeypatch, tmp_path):
    """Tasks is the supported path; it must be attempted first so a wheel
    carrying both never silently uses the deprecated one."""
    order = []

    class _FakeTasks(L._Backend):
        name = "mediapipe_tasks"

        def __init__(self, model_path, max_faces=1):
            super().__init__()
            order.append("tasks")

        def detect(self, frame_rgb):
            return _canonical_pts()

    class _FakeLegacy(L._Backend):
        name = "mediapipe_solutions"

        def __init__(self, max_faces=1):
            super().__init__()
            order.append("legacy")

        def detect(self, frame_rgb):
            return _canonical_pts()

    monkeypatch.setattr(L, "_TasksBackend", _FakeTasks)
    monkeypatch.setattr(L, "_LegacyBackend", _FakeLegacy)

    model = tmp_path / "face_landmarker.task"
    model.write_bytes(b"stub")
    fm = L.FaceMesh(model_path=model)
    assert order == ["tasks"], "legacy backend must not be constructed"
    assert fm.available is True
    assert fm.backend == "mediapipe_tasks"
    assert fm.reason == ""


def test_falls_through_to_legacy_and_records_why_tasks_failed(
        monkeypatch, tmp_path):
    tried = []

    class _DeadTasks(L._Backend):
        name = "mediapipe_tasks"

        def __init__(self, model_path, max_faces=1):
            super().__init__()
            tried.append("tasks")
            self.reason = "FaceLandmarker could not be created: boom"

        def detect(self, frame_rgb):
            return _canonical_pts()

    class _LiveLegacy(L._Backend):
        name = "mediapipe_solutions"

        def __init__(self, max_faces=1):
            super().__init__()
            tried.append("legacy")

        def detect(self, frame_rgb):
            return _canonical_pts()

    monkeypatch.setattr(L, "_TasksBackend", _DeadTasks)
    monkeypatch.setattr(L, "_LegacyBackend", _LiveLegacy)

    model = tmp_path / "face_landmarker.task"
    model.write_bytes(b"stub")
    fm = L.FaceMesh(model_path=model)
    assert tried == ["tasks", "legacy"]
    assert fm.backend == "mediapipe_solutions"
    dense = fm(_frame())
    assert dense is not None
    assert dense.points.shape == (L.CANONICAL_POINT_COUNT, 2)


def test_all_backends_failing_names_every_reason(monkeypatch, tmp_path):
    class _Dead(L._Backend):
        name = "dead_backend"

        def __init__(self, *a, **k):
            super().__init__()
            self.reason = f"{self.name} exploded"

        def detect(self, frame_rgb):
            return None

    monkeypatch.setattr(L, "_TasksBackend", _Dead)
    monkeypatch.setattr(L, "_LegacyBackend", _Dead)

    model = tmp_path / "face_landmarker.task"
    model.write_bytes(b"stub")
    fm = L.FaceMesh(model_path=model)
    assert fm.available is False
    assert fm.reason == ("dead_backend: dead_backend exploded; "
                         "dead_backend: dead_backend exploded")
    assert fm(_frame()) is None


def test_backend_exception_is_contained(monkeypatch, tmp_path):
    """A backend that throws mid-scan must not kill the worker."""

    class _Rude(L._Backend):
        name = "mediapipe_tasks"

        def __init__(self, model_path, max_faces=1):
            super().__init__()

        def detect(self, frame_rgb):
            raise RuntimeError("kaboom")

    monkeypatch.setattr(L, "_TasksBackend", _Rude)
    model = tmp_path / "m.task"
    model.write_bytes(b"stub")
    fm = L.FaceMesh(model_path=model)
    assert fm.available is True
    assert fm(_frame()) is None            # contained, no raise


def test_grey_ir_frames_are_accepted(monkeypatch, tmp_path):
    """IR sensors deliver single-channel frames; a 2D array must be
    broadcast, not crash a cvtColor."""
    seen = {}

    class _Spy(L._Backend):
        name = "mediapipe_tasks"

        def __init__(self, model_path, max_faces=1):
            super().__init__()

        def detect(self, frame_rgb):
            seen["shape"] = frame_rgb.shape
            seen["dtype"] = frame_rgb.dtype
            return _canonical_pts()

    monkeypatch.setattr(L, "_TasksBackend", _Spy)
    model = tmp_path / "m.task"
    model.write_bytes(b"stub")
    fm = L.FaceMesh(model_path=model)
    assert fm(np.zeros((360, 640), np.uint8)) is not None
    assert seen["shape"] == (360, 640, 3)
    assert seen["dtype"] == np.uint8
    assert fm(np.zeros((480, 640, 3), np.uint8)) is not None


def test_tasks_timestamps_are_strictly_increasing(monkeypatch):
    """VIDEO mode rejects a repeated timestamp, and camera frames can
    arrive inside the same millisecond."""
    seen = []

    class _Stub:
        def __init__(self):
            self._pts = _canonical_pts()

        def detect_for_video(self, image, ts):
            seen.append(ts)
            h, w = image.height, image.width

            class _P:
                def __init__(self, p):
                    self.x, self.y = p[0] / w, p[1] / h

            class _R:
                face_landmarks = [[_P(p) for p in self._pts]]

            return _R()

        def close(self):
            pass

    b = L._TasksBackend.__new__(L._TasksBackend)
    L._Backend.__init__(b)
    b._task = _Stub()
    b._t0 = 0.0
    b._last_ts = -1
    b.reason = ""

    # detect() needs mp.Image; the timestamp logic under test does not.
    monkeypatch.setitem(sys.modules, "mediapipe", _StubMP())

    for _ in range(5):
        b.detect(np.zeros((8, 8, 3), np.uint8))
    assert seen == sorted(seen)
    assert len(set(seen)) == len(seen), "timestamps must strictly increase"


class _StubMP:
    """Just enough of the mediapipe module for mp.Image / mp.ImageFormat."""

    class ImageFormat:
        SRGB = 0

    class Image:
        def __init__(self, image_format=None, data=None):
            self.height, self.width = data.shape[:2]


def test_tasks_backend_reports_a_missing_mediapipe(monkeypatch, tmp_path):
    """If the Tasks import itself fails, say so -- that is the difference
    between 'not installed' and 'broken install'."""
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *a, **k):
        if name.startswith("mediapipe"):
            raise ImportError("no mediapipe here")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    b = L._TasksBackend(tmp_path / "whatever.task")
    assert b.available is False
    assert "mediapipe Tasks API unavailable" in b.reason


def _frame():
    return np.zeros((480, 640, 3), np.uint8)


# ---- the real wheel, when it is there ---------------------------------

def test_real_backend_loads_the_pinned_model_when_present():
    """Integration: with mediapipe AND the pinned model on disk, a live
    backend must be selected. Skipped otherwise -- but on a packaged
    install this is the assertion that stops the silent regression from
    coming back."""
    import importlib.util

    if importlib.util.find_spec("mediapipe") is None:
        pytest.skip("mediapipe not installed")
    from faceid_vision.models import DEFAULT_MODEL_DIR

    model = DEFAULT_MODEL_DIR / "face_landmarker.task"
    if not model.exists():
        pytest.skip("face_landmarker.task not installed")

    fm = L.FaceMesh(model_path=model)
    try:
        assert fm.available is True, fm.reason
        assert fm.backend in L.BACKEND_ORDER
        # A frame with no face in it must return None, not raise.
        assert fm(_frame()) is None
    finally:
        fm.close()
