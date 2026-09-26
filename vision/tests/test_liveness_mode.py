"""Mode gating of RGB-only screen cues.

The glare/bezel/moire checks in liveness/screen.py are tuned against
colour webcam texture statistics and false-positive on IR sensor
noise. They must be skipped (not retuned) when the active camera mode
is "ir"; IR screen detection belongs to analyse_ir_face()'s
"screen_dark" cue instead.
"""
import numpy as np

from faceid_vision.camera import CameraFormat
from faceid_vision.detect import Face
from faceid_vision.liveness.screen import ScreenCues
from faceid_vision.scan import ScanConfig, ScanEngine


class _FakeDetector:
    def largest(self, frame):
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


class _FakeIRCam:
    """A stub secondary camera on a GREY/IR stream. If analyse_ir_face()
    ever reads it, its dark 2D frame satisfies screen_dark -- so any
    ir_screen_dark deny in an rgb-mode scan proves IR analysis ran when
    it must not."""

    def __init__(self):
        self.read_times = 0
        self._frame = np.full((360, 640), 30, np.uint8)

    def frames(self, timeout_s):
        self.read_times += 1
        yield 0.0, self._frame


def _engine():
    return ScanEngine(_FakeDetector(), _FakeEmbedder(), mesh=_FakeMesh())


def _scan(engine, mode, n=3):
    cfg = ScanConfig(mode=mode, min_embeddings=1, max_embeddings=2,
                     timeout_ms=100)
    return engine.scan(_FakeCam(n), cfg)


def _scan_with_ir(engine, mode, ir_cam):
    cfg = ScanConfig(mode=mode, min_embeddings=1, max_embeddings=2,
                     timeout_ms=100)
    return engine.scan(_FakeCam(3), cfg, ir_camera=ir_cam), ir_cam


def test_ir_mode_never_calls_screen_report(monkeypatch):
    """mode="ir" must not even invoke screen_report(): its moire/glare
    thresholds are webcam-tuned and false-positive on IR noise. The
    IR-primary native cue (analyse_ir_face on the primary frame) MAY
    still run — only RGB cues are forbidden here."""
    def _boom(*_a, **_k):
        raise AssertionError("screen_report() called in IR mode")
    monkeypatch.setattr("faceid_vision.scan.screen_report", _boom)
    # assess/align are not under test here; drop the pixel math.
    monkeypatch.setattr("faceid_vision.scan.assess",
                        lambda *a, **k: _ok_report())
    res = _scan(_engine(), mode="ir")
    assert res.error is None
    for cue in res.liveness.deny:
        assert cue in ("ir_screen_dark", "antispoof_model", "planar_photo")
    assert res.liveness.notes.get("moire") is None
    assert res.liveness.notes.get("glare_frac") is None
    assert len(res.embeddings) > 0


def test_rgb_mode_still_denies_on_moire(monkeypatch):
    """RGB mode keeps the full screen-report path: a real replay attack
    on a colour camera must still surface "moire" in deny_cues. Moire is
    a soft cue, so the scan needs moire_required usable frames before
    the veto lands; here the fake floods moire on all three frames."""
    captured = []

    def _fake_report(frame, face):
        captured.append(1)
        return ScreenCues(moire=True, moire_energy=0.2)
    monkeypatch.setattr("faceid_vision.scan.screen_report", _fake_report)
    monkeypatch.setattr("faceid_vision.scan.assess",
                        lambda *a, **k: _ok_report())
    cfg = ScanConfig(mode="rgb", min_embeddings=3, max_embeddings=8,
                     timeout_ms=100)
    res = _engine().scan(_FakeCam(3), cfg)
    assert len(captured) == 3                 # ran on all usable frames
    assert "moire" in res.liveness.deny
    assert res.liveness.notes["moire"] == 0.2
    assert res.liveness.notes["moire_window"]["hits"] == 3


def test_rgb_mode_ignores_ir_camera_entirely(monkeypatch):
    """A camera configured as "rgb" must not run IR analysis at all --
    even when an IR camera was opened alongside it (as the daemon does
    for every mode that has one). The configured mode decides which
    rules apply; an attached IR sensor, or a GREY-negotiated frame, must
    never promote an rgb scan to IR analysis."""
    captured = []

    def _fake_report(frame, face):
        captured.append(1)
        return ScreenCues(moire=False)
    monkeypatch.setattr("faceid_vision.scan.screen_report", _fake_report)
    monkeypatch.setattr("faceid_vision.scan.assess",
                        lambda *a, **k: _ok_report())
    eng = _engine()
    res, ir = _scan_with_ir(eng, mode="rgb", ir_cam=_FakeIRCam())
    assert res.error is None
    assert ir.read_times == 0                 # IR stream never even read
    assert captured                           # screen_report() still ran normally
    assert "ir_screen_dark" not in res.liveness.deny
    assert "ir_skin" not in res.liveness.confirm


def test_only_ir_and_both_modes_run_ir_analysis(monkeypatch):
    """"ir", "both" and their explicit alias "hybrid" are the only modes
    where analyse_ir_face() may run: the dark GREY stub must yield its
    ir_screen_dark deny."""
    monkeypatch.setattr("faceid_vision.scan.assess",
                        lambda *a, **k: _ok_report())
    for mode in ("ir", "both", "hybrid"):
        res, ir = _scan_with_ir(_engine(), mode=mode, ir_cam=_FakeIRCam())
        assert ir.read_times == 1
        assert "ir_screen_dark" in res.liveness.deny


def test_rgb_request_never_opens_ir_camera(monkeypatch):
    """Guard at the worker request layer, end to end: even a scan
    request that carries an ir_device field must not open the IR camera
    when its mode is "rgb". Mode is configuration; it wins over which
    sensor happens to be attached."""
    import contextlib
    import types

    from faceid_vision import __main__ as m
    from faceid_vision.fuse import LivenessState
    from faceid_vision.scan import ScanResult

    opened = []

    class _FakeCam:
        def __init__(self):
            self._frame = np.full((200, 200, 3), 128, np.uint8)

        def frames(self, timeout_s):
            yield 0.0, self._frame

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _FakeEngine:
        def __init__(self):
            self.seen = []

        def scan(self, camera, cfg, emit=None, ir_camera=None):
            self.seen.append((cfg.mode, ir_camera is not None))
            return ScanResult(
                embeddings=[np.array([1.0, 0.0, 0.0], np.float32)],
                model_id="m", liveness=LivenessState(), frames=1,
                usable=1, elapsed_ms=1)

    @contextlib.contextmanager
    def _fake_primary(dev, attempts=2):
        opened.append(dev)
        yield _FakeCam()

    monkeypatch.setattr(m, "_open_with_retry", _fake_primary)
    monkeypatch.setattr(m, "open_source", lambda dev, **kw: (opened.append(dev), _FakeCam())[1])

    eng = _FakeEngine()
    args = types.SimpleNamespace(source=None, device="/dev/video1",
                                 ir_device=None)

    out = m._handle_scan(eng, {"op": "scan", "id": "s", "mode": "rgb",
                               "ir_device": "/dev/video3",
                               "timeout_ms": 4000}, args, lambda _: None)
    assert out.get("ev") == "done"
    assert opened == ["/dev/video1"]            # IR node never opened
    assert eng.seen == [("rgb", False)]         # and never passed to scan

    opened.clear()
    eng.seen.clear()
    out = m._handle_scan(eng, {"op": "scan", "id": "s", "mode": "both",
                               "ir_device": "/dev/video3",
                               "timeout_ms": 4000}, args, lambda _: None)
    assert out.get("ev") == "done"
    assert "/dev/video3" in opened               # IR camera opened only for IR modes
    assert eng.seen == [("both", True)]

    opened.clear()
    eng.seen.clear()
    out = m._handle_scan(eng, {"op": "scan", "id": "s", "mode": "hybrid",
                               "ir_device": "/dev/video3",
                               "timeout_ms": 4000}, args, lambda _: None)
    assert out.get("ev") == "done"
    assert "/dev/video3" in opened               # hybrid opens IR for spoof liveness
    assert eng.seen == [("hybrid", True)]


def test_greyscale_only_classification(monkeypatch):
    """A GREY-only node is an IR/monochrome stream; anything advertising
    a colour format, or nothing probeable, stays RGB."""
    import faceid_vision.camera as cam

    monkeypatch.setattr(cam, "probe_formats",
                        lambda dev: [CameraFormat("GREY", 640, 360, (15,))])
    assert cam.is_greyscale_only("/dev/video3") is True

    monkeypatch.setattr(cam, "probe_formats",
                        lambda dev: [CameraFormat("MJPG", 640, 480, (30,)),
                                     CameraFormat("GREY", 640, 360, (15,))])
    assert cam.is_greyscale_only("/dev/video0") is False

    monkeypatch.setattr(cam, "probe_formats", lambda dev: [])
    assert cam.is_greyscale_only("/dev/video2") is False


def _ok_report():
    from faceid_vision.quality import QualityReport
    return QualityReport(True, "")