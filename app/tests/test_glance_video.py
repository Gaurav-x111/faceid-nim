"""Glance is real video end to end: the mp4 exists, the lock-screen
frame sequence is extracted from it, both extension copies reference
the frames, and the app can locate the clip for its Gtk.Video preview.

If the mp4 is replaced, re-run packaging/scripts/extract-glance-frames
and these tests confirm the sequence came along.
"""
import pathlib
import re

from faceid_app import openings

REPO = pathlib.Path(__file__).resolve().parents[2]
MP4 = REPO / "linux-anim" / "glance_anim" / "assets" / "unlockanimation.mp4"
FRAME_DIRS = (
    REPO / "shell-ext" / "faceid@nim" / "glance-frames",
    REPO / "faceid-nim-dynamic-island" / "faceid-nim" / "shell-ext"
    / "faceid@nim" / "glance-frames",
)
JS_FILES = (
    REPO / "shell-ext" / "faceid@nim" / "opening.js",
    REPO / "faceid-nim-dynamic-island" / "faceid-nim" / "shell-ext"
    / "faceid@nim" / "opening.js",
)


def _frames(d: pathlib.Path) -> list:
    return sorted(d.glob("frame-*.png"))


def test_source_clip_exists():
    assert MP4.is_file(), f"{MP4} missing — glance has no video"


def test_frame_sequences_extracted():
    for d in FRAME_DIRS:
        frames = _frames(d)
        assert len(frames) >= 20, (
            f"{d} holds {len(frames)} frames — re-run "
            "packaging/scripts/extract-glance-frames")
        # Contiguous from frame-001 with no gaps (the player probes
        # sequentially and stops at the first hole).
        for i, p in enumerate(frames, start=1):
            assert p.name == f"frame-{i:03d}.png", (
                f"{d}: expected frame-{i:03d}.png, found {p.name}")


def test_js_plays_frames():
    for path in JS_FILES:
        src = path.read_text()
        assert "glance-frames" in src, f"{path} never loads the frames"
        assert "videoWidget" in src, f"{path} has no video actor"


def test_app_finds_clip_for_preview():
    found = openings.glance_asset(openings.GLANCE_CLIPS["glance"])
    assert found, "app cannot locate unlockanimation.mp4 for preview"
    assert pathlib.Path(found).is_file()
