"""Unit tests for the parts that must be right regardless of hardware:
alignment, cosine/voting, EAR, planarity, protocol."""
import numpy as np
import pytest

from faceid_vision.align import ARC_REF, align
from faceid_vision.liveness.blink import BlinkDetector, eye_aspect_ratio
from faceid_vision.liveness.geometry import planarity_residual
from faceid_vision.matching import Voter, cosine_max
from faceid_vision.protocol import ProtocolError, decode, encode, validate_request


def test_align_maps_landmarks_onto_reference():
    """A known affine image of the reference must invert back to it."""
    M = np.array([[1.4, 0.0, 30.0], [0.0, 1.4, 20.0]], dtype=np.float32)
    lm = (ARC_REF @ M[:, :2].T) + M[:, 2]
    img = np.zeros((300, 300, 3), np.uint8)
    out = align(img, lm.astype(np.float32))
    assert out.shape == (112, 112, 3)


def test_cosine_max_picks_best_template():
    q = np.array([1.0, 0.0, 0.0], np.float32)
    T = np.array([[0.0, 1.0, 0.0], [0.9, 0.436, 0.0]], np.float32)
    T /= np.linalg.norm(T, axis=1, keepdims=True)
    assert cosine_max(q, T) == pytest.approx(0.9, abs=1e-3)
    assert cosine_max(q, np.empty((0, 3), np.float32)) == -1.0


def test_voter_needs_k_of_n():
    v = Voter(k=3, n=5, tau=0.4)
    for s in (0.9, 0.1, 0.9):
        v.push(s)
    assert not v.decided                      # only two passes
    v.push(0.95)
    assert v.decided
    assert v.best == pytest.approx(0.95)


def test_voter_window_forgets_old_frames():
    v = Voter(k=3, n=3, tau=0.4)
    for s in (0.9, 0.9, 0.1, 0.1):
        v.push(s)
    assert not v.decided


def test_ear_open_vs_closed():
    open_eye = np.array([[0, 0], [2, -3], [4, -3], [6, 0], [4, 3], [2, 3]], np.float32)
    closed = np.array([[0, 0], [2, -0.2], [4, -0.2], [6, 0], [4, 0.2], [2, 0.2]], np.float32)
    assert eye_aspect_ratio(open_eye) > 0.4
    assert eye_aspect_ratio(closed) < 0.1


def test_blink_requires_reopening():
    d = BlinkDetector()
    open_eye = np.array([[0, 0], [2, -3], [4, -3], [6, 0], [4, 3], [2, 3]], np.float32)
    closed = np.array([[0, 0], [2, -0.2], [4, -0.2], [6, 0], [4, 0.2], [2, 0.2]], np.float32)
    d.update(open_eye, open_eye)
    d.update(closed, closed)
    assert d.blinks == 0                      # still closed: not a blink yet
    d.update(open_eye, open_eye)
    assert d.blinks == 1


def test_blink_ignores_sustained_closure():
    """Someone asleep is not blinking."""
    d = BlinkDetector(max_closed_frames=3)
    open_eye = np.array([[0, 0], [2, -3], [4, -3], [6, 0], [4, 3], [2, 3]], np.float32)
    closed = np.array([[0, 0], [2, -0.2], [4, -0.2], [6, 0], [4, 0.2], [2, 0.2]], np.float32)
    d.update(open_eye, open_eye)
    for _ in range(10):
        d.update(closed, closed)
    d.update(open_eye, open_eye)
    assert d.blinks == 0


def test_planarity_flat_surface_has_near_zero_residual():
    """A synthetic plane under a homography must fit exactly."""
    rng = np.random.default_rng(0)
    pts = rng.uniform(0, 100, size=(60, 2)).astype(np.float32)
    H = np.array([[1.05, 0.02, 4.0], [0.01, 0.98, -3.0], [1e-4, 5e-5, 1.0]], np.float32)
    import cv2
    warped = cv2.perspectiveTransform(pts.reshape(-1, 1, 2), H).reshape(-1, 2)
    r = planarity_residual(pts, warped, eye_l=0, eye_r=1)
    assert r < 1e-3


def test_protocol_roundtrip_and_rejection():
    msg = {"op": "scan", "id": "s1", "timeout_ms": 4000}
    assert validate_request(decode(encode(msg))) == msg
    with pytest.raises(ProtocolError):
        validate_request({"op": "nope", "id": "x"})
    with pytest.raises(ProtocolError):
        validate_request({"op": "scan", "id": "x", "timeout_ms": 999999})
    with pytest.raises(ProtocolError):
        decode(b"{not json")
