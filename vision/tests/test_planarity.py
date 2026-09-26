import numpy as np

from faceid_vision.liveness.geometry import PlanarityTracker, planarity_residual


def test_tracker_uses_mediapipe_eye_corners():
    tracker = PlanarityTracker()
    assert tracker.eye_l == 33
    assert tracker.eye_r == 263


def test_residual_normalization_depends_on_eye_corners():
    gy, gx = np.mgrid[0:22, 0:22]
    points = np.stack([gx.ravel(), gy.ravel()], axis=1)[:468].astype(np.float32)
    points[33] = [20.0, 30.0]
    points[263] = [120.0, 30.0]
    points[0] = [70.0, 50.0]
    points[1] = [71.0, 51.0]
    moved = points.copy()
    moved[:, 0] += 5.0
    moved[40, 1] += 2.0

    correct = planarity_residual(points, moved, 33, 263)
    wrong = planarity_residual(points, moved, 0, 1)
    assert np.isfinite(correct)
    assert np.isfinite(wrong)
    assert not np.isclose(correct, wrong)
