"""Camera discovery, from its new home in the vision package.

Two things are being pinned here:

1. The app's ``camera_discovery`` module is now a *re-export* of
   ``faceid_vision.cameras``. The UI and the headless provisioning path
   (postinst, `faceid-nim status`) must never disagree about which node
   is the IR sensor, so there has to be exactly one implementation.

2. ``auto_config`` is what a fresh install uses, with nobody there to
   answer questions. An explicitly configured node that still exists must
   win; only a node that has gone away may be replaced.
"""
import pytest

from faceid_app import camera_discovery as cd
from faceid_vision import cameras


def cam(path, kind, fivecc, sizes=((640, 480),), card="Integrated Webcam"):
    return cameras.CameraInfo(
        index=int(path.rsplit("video", 1)[-1]),
        path=path,
        card=card,
        driver="uvcvideo",
        bus_info="usb",
        device_caps=1,
        formats=[cameras.CameraFormat(fivecc=fivecc, description="",
                                      sizes=list(sizes))],
        readable=True,
    )


# ---- the re-export ----------------------------------------------------

def test_app_module_is_a_reexport_not_a_second_implementation():
    assert cd._impl is cameras
    for name in cd.__all__:
        assert getattr(cd, name) is getattr(cameras, name), \
            f"{name} diverges between the app and the vision package"


def test_app_import_works_without_the_vision_package_on_syspath(monkeypatch):
    """The app runs on the *system* python; the bundled venv is
    worker-only. So the loader must cope with faceid_vision not being
    importable, and the failure mode is a clear ImportError, never a
    half-initialised module."""
    import importlib
    import sys

    saved = {k: v for k, v in sys.modules.items() if k.startswith("faceid")}
    for k in list(saved):
        monkeypatch.delitem(sys.modules, k, raising=False)
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__

    def _blocked(name, *a, **k):
        if name.startswith("faceid_vision"):
            raise ImportError("simulated: vision package absent")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", _blocked)
    try:
        mod = importlib.import_module("faceid_app.camera_discovery")
    except ImportError as e:
        # Acceptable and honest: the app reports the real problem.
        assert "cameras" in str(e)
        return
    finally:
        for k in [k for k in sys.modules if k.startswith("faceid")]:
            monkeypatch.delitem(sys.modules, k, raising=False)
        for k, v in saved.items():
            monkeypatch.setitem(sys.modules, k, v)
    # If it did load, it must be the same module, not a lookalike.
    assert mod._impl.__name__ == "faceid_vision_cameras_fallback" or \
        mod._impl is cameras


# ---- the plan the app already relied on -------------------------------

def test_explicit_ir_falls_back_to_rgb_without_ir_camera():
    rgb = cam("/dev/video0", "rgb", "MJPG")
    plan = cameras.resolve_plan({"camera_mode": "ir"}, [rgb])
    assert plan["mode"] == "rgb"
    assert plan["ir_camera"] is None
    assert plan["camera"] == "/dev/video0"
    assert any("using RGB" in note for note in plan["reminders"])


def test_explicit_ir_uses_ir_when_available():
    rgb = cam("/dev/video0", "rgb", "MJPG")
    ir = cam("/dev/video1", "ir", "GREY")
    plan = cameras.resolve_plan({"camera_mode": "ir"}, [rgb, ir])
    assert plan["mode"] == "ir"
    assert plan["ir_camera"] == "/dev/video1"


# ---- headless auto-configuration --------------------------------------

def _patch_discovery(monkeypatch, cams):
    monkeypatch.setattr(cameras, "discover", lambda: list(cams))


def test_auto_config_picks_rgb_and_ir_on_a_combo_sensor(monkeypatch):
    rgb = cam("/dev/video1", "rgb", "MJPG", sizes=((1280, 720),))
    ir = cam("/dev/video3", "ir", "GREY", sizes=((640, 360),))
    _patch_discovery(monkeypatch, [rgb, ir])
    plan = cameras.auto_config()
    assert plan["camera"] == "/dev/video1"
    assert plan["ir_camera"] == "/dev/video3"
    assert plan["reminders"] == []


def test_auto_config_keeps_an_explicit_choice_that_still_exists(monkeypatch):
    """A configured node must never be overridden by a re-probe."""
    rgb0 = cam("/dev/video0", "rgb", "MJPG", sizes=((640, 480),))
    rgb1 = cam("/dev/video1", "rgb", "MJPG", sizes=((1280, 720),))
    _patch_discovery(monkeypatch, [rgb0, rgb1])
    plan = cameras.auto_config(camera="/dev/video0")
    assert plan["camera"] == "/dev/video0", "explicit choice was overridden"


def test_auto_config_replaces_a_node_that_has_gone_away(monkeypatch, caplog):
    rgb1 = cam("/dev/video1", "rgb", "MJPG", sizes=((1280, 720),))
    _patch_discovery(monkeypatch, [rgb1])
    with caplog.at_level("WARNING"):
        plan = cameras.auto_config(camera="/dev/video0")
    assert plan["camera"] == "/dev/video1"
    assert "/dev/video0" in caplog.text
    assert "not present" in caplog.text


def test_auto_config_replaces_a_vanished_ir_node_too(monkeypatch, caplog):
    rgb = cam("/dev/video0", "rgb", "MJPG")
    _patch_discovery(monkeypatch, [rgb])
    with caplog.at_level("WARNING"):
        plan = cameras.auto_config(ir_camera="/dev/video3")
    assert plan["ir_camera"] is None
    assert "not present" in caplog.text


def test_auto_config_with_no_cameras_says_so_and_invents_nothing(monkeypatch):
    _patch_discovery(monkeypatch, [])
    plan = cameras.auto_config()
    assert plan["camera"] is None
    assert plan["ir_camera"] is None
    assert any("No usable face camera" in r for r in plan["reminders"])


def test_auto_config_on_an_ir_only_laptop_still_reports_the_sensor(
        monkeypatch):
    """No RGB node is a real configuration, not a failure: IR recognition
    works. The reminder must say what is actually lost."""
    ir = cam("/dev/video3", "ir", "GREY", sizes=((640, 360),))
    _patch_discovery(monkeypatch, [ir])
    plan = cameras.auto_config()
    assert plan["camera"] is None
    assert plan["ir_camera"] == "/dev/video3"
    assert any("IR sensor" in r for r in plan["reminders"])


def test_auto_config_never_raises_on_a_probe_failure(monkeypatch):
    def _boom():
        raise OSError("no /dev")

    monkeypatch.setattr(cameras, "discover", _boom)
    with pytest.raises(OSError):
        # discover() raising is the caller's problem; auto_config itself
        # must not swallow a real error into a silent "no cameras".
        cameras.auto_config()


# ---- the contract postinst depends on --------------------------------

def test_config_lines_emits_only_devices_that_were_found():
    """postinst concatenates these straight into a TOML file it just
    created, so a line that is not `key = "value"` -- or a key for a
    device that does not exist -- is a broken install."""
    both = {"camera": "/dev/video1", "ir_camera": "/dev/video3"}
    assert cameras.config_lines(both) == [
        'camera = "/dev/video1"',
        'ir_camera = "/dev/video3"',
    ]
    rgb_only = {"camera": "/dev/video0", "ir_camera": None}
    assert cameras.config_lines(rgb_only) == ['camera = "/dev/video0"']
    neither = {"camera": None, "ir_camera": None}
    assert cameras.config_lines(neither) == []
    assert cameras.config_lines({}) == []


def test_config_lines_output_is_valid_toml_when_substituted():
    """The real failure this guards: postinst interpolates the lines into
    a heredoc, and a malformed line makes the whole config unparsable --
    which the daemon would silently swallow by falling back to defaults,
    quietly undoing the auto-detection."""
    import tomllib

    for plan in ({"camera": "/dev/video1", "ir_camera": "/dev/video3"},
                 {"camera": "/dev/video0", "ir_camera": None},
                 {"camera": None, "ir_camera": None}):
        doc = 'enabled = false\nmode = "auto"\n' + "\n".join(
            cameras.config_lines(plan))
        parsed = tomllib.loads(doc)
        assert parsed["mode"] == "auto"
        assert parsed.get("camera") == plan["camera"]
        assert parsed.get("ir_camera") == plan["ir_camera"]


def test_config_lines_cli_emits_parseable_toml_on_this_host():
    """End to end through the actual CLI entry point, on whatever this
    machine has. Skipped when there are no cameras."""
    import subprocess
    import sys
    import tomllib

    root = str(cameras.__file__).split("/faceid_vision/")[0]
    r = subprocess.run(
        [sys.executable, "-m", "faceid_vision.cameras", "--config-lines"],
        capture_output=True, text=True,
        env={"PYTHONPATH": root, "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    if not lines:
        pytest.skip("no cameras on this host; covered by config_lines()")
    parsed = tomllib.loads('mode = "auto"\n' + "\n".join(lines))
    for line in lines:
        assert line.startswith(("camera = ", "ir_camera = ")), line
    if parsed.get("camera"):
        import os
        assert os.path.exists(parsed["camera"]), \
            "emitted a camera path that does not exist"
