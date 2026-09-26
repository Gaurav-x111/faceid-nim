"""IR liveness cues, and one helper that is deliberately not wired in.

`emitter_difference` is the strongest cheap anti-spoof test on IR
hardware, and it has no caller: the worker only ever sees one stream, so
nothing in the unlock path can produce the emitter-off/emitter-on pair it
compares. It is kept and tested here so that (a) nobody reports it as an
active check, and (b) it is correct the day it does get wired.
"""
import numpy as np

from faceid_vision.liveness.ir import analyse_ir_face, emitter_difference


# The real decision rule, which these tests document rather than invent:
#   screen_dark     = luma < 25  OR  contrast < 12
#   skin_response_ok= luma >= 35 AND  contrast >= 18
# so the "unknown" region is contrast in [12, 18), or textured-but-dark.
# Uniform noise of amplitude A has std ~= A/sqrt(3), which is why the
# amplitudes below are ~30 and not ~10: +/-10 only reaches std 5.8 and
# reads as a flat screen.

def _texture(base: int, amplitude: int, seed: int = 3) -> np.ndarray:
    noise = np.random.default_rng(seed).integers(-amplitude, amplitude + 1,
                                                 (120, 120))
    return np.clip(base + noise, 0, 255).astype(np.uint8)


def test_analyse_ir_flags_a_bright_textured_face_as_skin():
    """Bright (luma >= 35) and textured (contrast >= 18) = skin, the live
    case. This is the confirm cue that stands in for a passive
    anti-spoof model on IR hardware."""
    rep = analyse_ir_face(_texture(150, 40), (0, 0, 120, 120))
    assert rep.available is True
    assert rep.mean_lum >= 35
    assert rep.contrast >= 18
    assert rep.screen_dark is False
    assert rep.skin_response_ok is True


def test_analyse_ir_flags_a_dark_flat_screen():
    """Dark + flat = a screen or print replay. This is the IR-native
    replacement for the RGB moire/glare gates, which are gated off in IR
    mode on purpose."""
    rep = analyse_ir_face(np.full((120, 120), 12, np.uint8), (0, 0, 120, 120))
    assert rep.screen_dark is True
    assert rep.skin_response_ok is False, \
        "screen_dark must win; the two are mutually exclusive"


def test_analyse_ir_flags_a_bright_flat_screen():
    """A backlit screen at full brightness is still a screen: flat is
    what gives it away, not darkness. Luma alone must not clear it."""
    rep = analyse_ir_face(np.full((120, 120), 200, np.uint8), (0, 0, 120, 120))
    assert rep.mean_lum >= 35, "precondition: this one IS bright"
    assert rep.screen_dark is True


def test_analyse_ir_dead_band_on_contrast_reports_unknown():
    """Contrast in [12, 18) is the unknown region: neither flag may fire,
    because a wrong confirm is as bad as a wrong veto."""
    rep = analyse_ir_face(_texture(150, 22), (0, 0, 120, 120))
    assert 12 <= rep.contrast < 18, f"contrast was {rep.contrast}"
    assert rep.screen_dark is False
    assert rep.skin_response_ok is False


def test_analyse_ir_dim_but_textured_is_unknown_not_a_confirm():
    """Textured, but not bright enough to be skin (25 <= luma < 35).
    Low light must not manufacture a confirm, and must not be called a
    screen either."""
    rep = analyse_ir_face(_texture(30, 40), (0, 0, 120, 120))
    assert 25 <= rep.mean_lum < 35, f"luma was {rep.mean_lum}"
    assert rep.contrast >= 18, f"contrast was {rep.contrast}"
    assert rep.skin_response_ok is False
    assert rep.screen_dark is False


def test_analyse_ir_dark_anything_is_a_screen():
    """Below luma 25 the answer is screen regardless of texture: a screen
    in a dark room is dark, and that is the attack this catches."""
    rep = analyse_ir_face(_texture(15, 40), (0, 0, 120, 120))
    assert rep.mean_lum < 25
    assert rep.screen_dark is True


def test_analyse_ir_on_an_empty_crop_is_available_but_silent():
    """A box outside the frame must not crash and must not deny."""
    rep = analyse_ir_face(np.full((100, 100), 200, np.uint8), (500, 500, 50, 50))
    assert rep.available is True
    assert rep.screen_dark is False
    assert rep.skin_response_ok is False


def test_analyse_ir_accepts_a_grey_ir_frame():
    """IR sensors hand us single-channel frames; a 3-channel-only
    implementation would crash the scan. Same verdict as the BGR
    equivalent is what matters."""
    grey = _texture(150, 40)
    rep = analyse_ir_face(grey, (0, 0, 120, 120))
    assert rep.available is True
    assert rep.skin_response_ok is True


# ---- the unwired helper ------------------------------------------------

def test_emitter_difference_fires_for_a_real_face():
    off = np.full((200, 200, 3), 20, np.uint8)
    on = off.copy()
    on[50:150, 50:150] = 120          # emitter lights the face up
    assert emitter_difference(on, off, (50, 50, 100, 100)) is True


def test_emitter_difference_rejects_a_screen_replay():
    """A screen emits almost no infrared, so switching the emitter on
    barely changes it. This is the property the whole function exists
    for."""
    off = np.full((200, 200, 3), 20, np.uint8)
    on = off.copy()
    on[50:150, 50:150] = 26           # barely moves
    assert emitter_difference(on, off, (50, 150, 100, 100)) is False


def test_emitter_difference_handles_mismatched_or_empty_crops():
    a = np.full((200, 200, 3), 20, np.uint8)
    b = np.full((100, 100, 3), 200, np.uint8)
    assert emitter_difference(a, a, (900, 900, 10, 10)) is False   # empty crop
    assert emitter_difference(a, b, (0, 0, 50, 50)) is False       # size mismatch


def test_emitter_difference_handles_grey_frames():
    off = np.full((200, 200), 20, np.uint8)
    on = off.copy()
    on[50:150, 50:150] = 120
    assert emitter_difference(on, off, (50, 50, 100, 100)) is True


def test_emitter_difference_is_not_called_by_the_scan_path():
    """If this ever starts failing, someone wired the helper into a scan
    without the emitter-toggle sequence it requires. That would report a
    check that cannot work -- the exact failure mode this project keeps
    guarding against. Re-point this test if that is done deliberately."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "faceid_vision"
    for py in root.rglob("*.py"):
        if py.name in ("ir.py", "ir_emitter.py"):
            continue
        text = py.read_text()
        assert "emitter_difference(" not in text, \
            f"{py.name} calls emitter_difference(); the scan path has only " \
            "one stream and cannot satisfy it"
