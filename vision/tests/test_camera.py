"""Camera negotiation: format discovery and advertised-combo selection.

These tests are hermetic: the V4L2 probe is stubbed so they run with
no camera present. They pin the behaviour that keeps a GREY-only IR
device (640x360 @ 15fps) from being asked for MJPG 640x480 @ 30fps.
"""
import pytest

from faceid_vision.camera import (CameraConfig, CameraFormat, open_source,
                                  pick_format, probe_formats)


IR_LIST = """\
ioctl: VIDIOC_ENUM_FMT
\tType: Video Capture

\t[0]: 'GREY' (8-bit Greyscale)
\t\tSize: Discrete 640x360
\t\t\tInterval: Discrete 0.067s (15.000 fps)
"""

RGB_LIST = """\
\t[0]: 'MJPG' (Motion-JPEG, compressed)
\t\tSize: Discrete 1920x1080
\t\t\tInterval: Discrete 0.033s (30.000 fps)
\t\tSize: Discrete 640x480
\t\t\tInterval: Discrete 0.033s (30.000 fps)
\t[1]: 'YUYV' (YUYV 4:2:2)
\t\tSize: Discrete 640x480
\t\t\tInterval: Discrete 0.033s (30.000 fps)
"""

# A v4l2-loopback/dummy driver reports only continuous sizes: no discrete
# match is possible, so negotiation must fall back to driver defaults.
CONTINUOUS_LIST = """\
\t[0]: 'BGR3' (24-bit BGR 8-8-8)
\t\tSize: Continuous 2x1 - 8192x8192
\t[1]: 'GREY' (8-bit Greyscale)
\t\tSize: Continuous 2x1 - 8192x8192
"""


def test_probe_formats_parses_ir_device(monkeypatch):
    monkeypatch.setattr(
        "faceid_vision.camera._run_v4l2",
        lambda dev, *args: IR_LIST.splitlines() if args == ("--list-formats-ext",) else None)
    fmts = probe_formats("/dev/video3")
    assert fmts == [CameraFormat(fourcc="GREY", width=640, height=360, fps=(15,))]


def test_probe_formats_parses_multiple(monkeypatch):
    monkeypatch.setattr(
        "faceid_vision.camera._run_v4l2",
        lambda dev, *args: RGB_LIST.splitlines() if args == ("--list-formats-ext",) else None)
    fmts = probe_formats("/dev/video1")
    assert CameraFormat("MJPG", 640, 480, (30,)) in fmts
    assert CameraFormat("YUYV", 640, 480, (30,)) in fmts


def test_probe_ignores_continuous_only_driver(monkeypatch):
    monkeypatch.setattr(
        "faceid_vision.camera._run_v4l2",
        lambda dev, *args: CONTINUOUS_LIST.splitlines())
    assert probe_formats("/dev/video0") == []


def test_pick_exact_advertised_combo_wins():
    cfg = CameraConfig(device="/dev/video1", width=640, height=480, fps=30,
                       fourcc="MJPG")
    fmts = [CameraFormat("MJPG", 1920, 1080, (30,)),
            CameraFormat("MJPG", 640, 480, (30,)),
            CameraFormat("YUYV", 640, 480, (30,))]
    assert pick_format(cfg, fmts) == CameraFormat("MJPG", 640, 480, (30,))


def test_pick_ir_device_falls_back_to_its_only_combo():
    cfg = CameraConfig(device="/dev/video3", width=640, height=480, fps=30,
                       fourcc="MJPG")
    fmts = [CameraFormat("GREY", 640, 360, (15,))]
    assert pick_format(cfg, fmts) == CameraFormat("GREY", 640, 360, (15,))


def test_pick_returns_none_when_nothing_advertised():
    cfg = CameraConfig(device="/dev/video0", fourcc="BGR3")
    assert pick_format(cfg, []) is None
    assert pick_format(cfg, [CameraFormat("GREY", None, None)]) is None


def test_open_source_files_vs_devices():
    from faceid_vision.camera import CameraConfig as Cfg
    cam = open_source("/dev/video3")
    assert cam.cfg.is_file is False
    assert cam.cfg.device == "/dev/video3"
    cam = open_source("clip.mp4")
    assert cam.cfg.is_file is True
    assert isinstance(cam.cfg, Cfg)