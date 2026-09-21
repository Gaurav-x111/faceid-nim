"""Moire detection is a prominence measurement, resolved over a rolling
window of usable frames.

The previous detector denied on the *fraction* of FFT energy in a
mid-frequency annulus; a real webcam scene measures ~0.15 against a
0.055 threshold, so genuine content was always refused. Moire from a
photographed pixel grid is not broadband energy, it is one dominant
sharp spatial frequency. The detector now reports peak-to-median
prominence in that annulus and the scan layer treats moire as a soft
cue: it is only vetoed once present in enough of the last N frames.
"""
import numpy as np
import pytest

from faceid_vision.detect import Face
from faceid_vision.liveness.screen import MOIRE_THRESHOLD, ScreenCues, screen_report
from faceid_vision.quality import QualityReport
from faceid_vision.scan import ScanConfig, ScanEngine


def _face():
    return Face(box=(0, 0, 200, 200),
                landmarks=np.zeros((5, 2), dtype=np.float32),
                score=0.99)


def _grid_face(pitch: int, amp: float = 90.0):
    base = np.full((200, 200, 3), 80, dtype=np.float32)
    off = np.indices((200, 200))
    mask = ((off[0] // pitch + off[1] // pitch) % 2) == 0
    base[mask] += amp
    return np.clip(base, 0, 255).astype(np.uint8)


# ---- detector level -------------------------------------------------

def test_genuine_texture_is_not_moire():
    """Skin + mild sensor noise is broadband and flat in the annulus;
    the degraded office scene measured ~4.6x baseline prominence while
    the old broadband metric sat at 0.15 against a 0.055 veto."""
    rng = np.random.default_rng(7)
    y, x = np.mgrid[0:200, 0:200]
    smooth = 128 + 12 * np.sin(y / 38.0) * np.cos(x / 23.0)
    smooth += rng.normal(0, 1.5, (200, 200))
    img = np.clip(np.stack([smooth] * 3, axis=-1), 0, 255).astype(np.uint8)
    cues = screen_report(img, _face())
    assert not cues.moire
    assert cues.moire_energy < MOIRE_THRESHOLD


def test_pixel_grid_is_moire():
    for pitch in (3, 4, 6, 8):
        cues = screen_report(_grid_face(pitch), _face())
        assert cues.moire, f"grid pitch {pitch} not detected"
        assert cues.moire_energy >= MOIRE_THRESHOLD


def test_jpeg_block_periodic_is_moire():
    """An 8px block lattice -- as on a printed JPEG photo -- is periodic
    in the annulus and must be denied, not mistaken for benign texture."""
    base = np.full((200, 200, 3), 60, dtype=np.uint8)
    ii, jj = np.indices((200, 200))
    base[(ii % 8) < 4] += 40
    base[(jj % 8) < 4] += 40
    cues = screen_report(base, _face())
    assert cues.moire
    assert cues.moire_energy >= MOIRE_THRESHOLD


def test_flat_crop_scores_zero():
    """A flat, featureless crop has no spectral structure: there is
    nothing to judge, so it must not be scored as moire."""
    flat = np.full((200, 200, 3), 128, dtype=np.uint8)
    cues = screen_report(flat, _face())
    assert not cues.moire
    assert cues.moire_energy == 0.0


def test_high_contrast_edges_but_no_grid_pass():
    """Strong real edges (hairline, collar, window frame) are broadband,
    not a single dominant grid peak -- deny must stay off. This
    deterministic random-segment scene (no periodicity anywhere) scores
    ~13, safely under the 20 threshold."""
    rng = np.random.default_rng(11)
    img = np.full((200, 200, 3), 96, np.uint8)
    for _ in range(60):
        y0, x0 = rng.integers(10, 190, 2)
        ang = rng.uniform(0, 3.14)
        L = rng.integers(6, 40)
        for s in range(L):
            yy = int(y0 + s * np.sin(ang))
            xx = int(x0 + s * np.cos(ang))
            if 0 <= yy < 200 and 0 <= xx < 200:
                img[max(0, yy - 1):yy + 2, max(0, xx - 1):xx + 2] = 235
    for (cy, cx, rr) in [(60, 150, 22), (170, 40, 14), (35, 60, 10)]:
        yy, xx = np.indices((200, 200))
        img[np.hypot(xx - cx, yy - cy) < rr] = 45
    img = np.clip(img.astype(np.float32) + rng.normal(0, 1.8, img.shape),
                  0, 255).astype(np.uint8)
    cues = screen_report(img, _face())
    assert not cues.moire
    assert cues.moire_energy < MOIRE_THRESHOLD


def test_threshold_is_documented_and_stable():
    assert MOIRE_THRESHOLD == 20.0


# ---- scan-level temporal aggregation --------------------------------

class _FakeDetector:
    def __init__(self, n):
        self._n = n

    def largest(self, frame):
        if self._n == 0:
            return None
        self._n -= 1
        return Face(box=(40, 40, 120, 120),
                    landmarks=np.array(
                        [[62, 88], [98, 88], [80, 116], [60, 134], [100, 134]],
                        np.float32),
                    score=0.95)


class _FakeEmbedder:
    size = (112, 112)
    model_id = "fake_model"

    def __call__(self, crop):
        return np.array([1.0, 0.0, 0.0], np.float32)


class _FakeMesh:
    available = False

    def __call__(self, frame):
        return None


class _FakeCam:
    def __init__(self, n):
        self._frame = np.full((200, 200, 3), 128, np.uint8)
        self._n = n

    def frames(self, timeout_s):
        for _ in range(self._n):
            yield 0.0, self._frame


def _engine():
    return ScanEngine(_FakeDetector(6), _FakeEmbedder(), mesh=_FakeMesh())


def _seq_responses(moire_flags):
    flags = list(moire_flags)

    def fake(frame, face):
        hit = flags.pop(0) if flags else False
        return ScreenCues(moire=hit, moire_energy=200.0 if hit else 2.0)

    return fake


def _scan_window(engine, monkeypatch, flags):
    monkeypatch.setattr("faceid_vision.scan.screen_report", _seq_responses(flags))
    monkeypatch.setattr("faceid_vision.scan.assess",
                        lambda *a, **k: QualityReport(True, ""))
    cfg = ScanConfig(mode="rgb", min_embeddings=6, max_embeddings=8,
                     embed_every=1, timeout_ms=100)
    return engine.scan(_FakeCam(6), cfg)


@pytest.fixture
def patched(monkeypatch):
    return monkeypatch


def test_single_transient_moire_frame_does_not_veto(patched):
    res = _scan_window(_engine(), patched,
                       [False, True, False, False, False, False])
    assert "moire" not in res.liveness.deny
    assert res.liveness.notes["moire"] == 2.0
    assert res.error is None
    win = res.liveness.notes["moire_window"]
    assert win["hits"] == 1
    assert win["frames"] == 5


def test_persistent_moire_vetoes(patched):
    res = _scan_window(_engine(), patched,
                       [False, True, True, True, False, False])
    assert "moire" in res.liveness.deny


def test_sporadic_moire_never_vetoes(patched):
    res = _scan_window(_engine(), patched,
                       [True, False, False, False, True, False])
    assert "moire" not in res.liveness.deny


def test_glare_stays_hard_with_moire_soft(patched):
    """glare is a hard insta-deny and must stop the scan immediately,
    unlike moire which needs the window to fill."""

    def fake(frame, face):
        return ScreenCues(glare=True, glare_frac=0.9, moire=False)

    def boom(*_a, **_k):
        raise AssertionError("scan continued past a hard glare veto")

    patched.setattr("faceid_vision.scan.screen_report", fake)
    patched.setattr("faceid_vision.scan.assess",
                    lambda *a, **k: QualityReport(True, ""))
    cfg = ScanConfig(mode="rgb", min_embeddings=6, max_embeddings=8,
                     embed_every=1, timeout_ms=100)
    res = _engine().scan(_FakeCam(6), cfg)
    assert "glare" in res.liveness.deny


def test_required_and_threshold_reported(patched):
    res = _scan_window(_engine(), patched,
                       [False, True, False, False, False, False])
    win = res.liveness.notes["moire_window"]
    assert win["required"] == 3
    assert res.liveness.notes["moire_threshold"] == MOIRE_THRESHOLD