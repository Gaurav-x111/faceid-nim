"""UI/animation wiring regression tests (no display needed).

Covers Phase 5-6 + Phase 2 state model + preview-startup fix:
- sidebar order matches the required primary structure
- GitHub link constants + open_url abstraction (mocked launcher)
- GitHub icon resolves to the bundled mark (or theme fallback)
- no promotional creator attribution in app sources
- canonical scanning/recognized/denied mapping
- daemon-free preview plans for all three states (no D-Bus touched)
- daemon-down message is actionable and never claims readiness
- scan-face value/label separation (stored values never leak labels)
- install_package refuses silent overwrite without overwrite=True
"""
import inspect
import json
import os
import zipfile

from faceid_app import main, openings


def test_pages_order():
    names = [n for n, _l, _i in main.PAGES]
    assert names == ["overview", "identities", "cameras", "security",
                     "settings", "diagnostics"]
    assert "opening" not in names


def test_github_link():
    assert main.GITHUB_URL == "https://github.com/Gaurav-x111/faceid-nim"
    assert "releases" in main.GITHUB_TOOLTIP or "Releases" in main.GITHUB_TOOLTIP \
        or "releases" in main.GITHUB_TOOLTIP.lower()
    seen = []
    assert main.open_url(main.GITHUB_URL, launcher=seen.append) is True
    assert seen == [main.GITHUB_URL]
    # Non-http schemes refused, no launcher called
    assert main.open_url("file:///etc/passwd", launcher=seen.append) is False
    assert main.open_url("javascript:alert(1)", launcher=seen.append) is False
    assert len(seen) == 1


def test_preview_state_mapping():
    assert main.canonical_preview_state("waking") == "scanning"
    assert main.canonical_preview_state("searching") == "scanning"
    assert main.canonical_preview_state("verifying") == "scanning"
    assert main.canonical_preview_state("matched") == "recognized"
    assert main.canonical_preview_state("rejected") == "denied"
    assert main.canonical_preview_state("timeout") == "denied"
    assert main.canonical_preview_state("camera_error") == "denied"
    # Unknown/empty never fabricates success
    assert main.canonical_preview_state("bogus") == "scanning"
    assert main.canonical_preview_state("") == "scanning"
    assert set(main.PREVIEW_STATES) == {"scanning", "recognized", "denied"}


def test_scan_face_values():
    assert main.SCAN_FACE_VALUES == ["apple", "arena", "classic"]
    # Labels are UI-only; stored values are lowercase ids
    for v in main.SCAN_FACE_VALUES:
        assert v == v.lower()
        assert " " not in v and "(" not in v


def _make_package(tmp_path, vid="myanim"):
    pkg = tmp_path / "a.faceopen"
    meta = {"id": vid, "name": "My Anim", "bg": "#112233",
            "accent": "#445566", "ease": "outCubic", "fade_ms": 420}
    with zipfile.ZipFile(pkg, "w") as z:
        z.writestr("opening.json", json.dumps(meta))
    return pkg


def test_install_refuses_overwrite(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    pkg = _make_package(tmp_path)
    vid = openings.install_package(str(pkg))
    assert vid == "myanim"
    # Second install of same id without overwrite=True must refuse
    try:
        openings.install_package(str(pkg))
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "already exists" in str(e)
    # Explicit overwrite succeeds
    assert openings.install_package(str(pkg), overwrite=True) == "myanim"


def _cfg_with_builtin():
    cfg = openings.load()
    cfg.setdefault("variants", {})
    return cfg


def test_preview_plans_need_no_daemon(monkeypatch):
    """All three canned states resolve without touching D-Bus.

    Fails if preview_plan_for_state ever calls the daemon: the proxy
    constructor is replaced with a bomb.
    """
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gio

    def _bomb(*a, **k):
        raise AssertionError("preview must not touch D-Bus")

    monkeypatch.setattr(Gio.DBusProxy, "new_for_bus_sync", _bomb)
    cfg = _cfg_with_builtin()
    for state in ("scanning", "recognized", "denied"):
        plan = main.preview_plan_for_state("iphone", state, cfg)
        assert plan["kind"] in ("video", "cairo")
        assert plan["heading"] and plan["body"]
    # Unknown state never fabricates success; unknown variant is honest
    assert main.preview_plan_for_state(
        "iphone", "bogus", cfg)["kind"] in ("video", "cairo")
    none_plan = main.preview_plan_for_state("none", "scanning", cfg)
    assert none_plan["kind"] == "none"


def test_preview_plan_source_of_preview_plan():
    src = inspect.getsource(main.Window._preview_selected_state)
    assert "preview_plan_for_state" in src
    # No daemon calls on this path: no bus method, no daemon attribute,
    # no proxy construction (the word "daemon" may still appear in the
    # docstring explaining independence).
    assert "preview_animation" not in src
    assert "self.daemon" not in src
    assert "DBusProxy" not in src


def test_service_unknown_maps_to_friendly():
    """ServiceUnknown (and NameHasNoOwner) show the friendly line with
    the raw GDBus text kept as the collapsible detail — never masked,
    never presented as readiness."""
    for raw in (
            "GDBus.Error:org.freedesktop.DBus.Error.ServiceUnknown: "
            "The name org.faceidnim.Daemon1 was not provided by any "
            ".service files",
            "GDBus.Error:org.freedesktop.DBus.Error.NameHasNoOwner: "
            "Could not activate remote peer",
            "cannot reach faceid-nimd: ServiceUnknown"):
        friendly, technical = main.split_daemon_error(raw)
        assert friendly == ("The face unlock service isn't running. "
                            "Run: faceid-nim status")
        assert technical == raw
    # Unrelated failures pass through unchanged: no masking.
    friendly, technical = main.split_daemon_error(
        "GDBus.Error:org.freedesktop.DBus.Error.AccessDenied: nope")
    assert "AccessDenied" in friendly
    friendly, technical = main.split_daemon_error("")
    assert friendly and technical == ""


def test_github_icon_and_link():
    assert main.GITHUB_URL == "https://github.com/Gaurav-x111/faceid-nim"
    path = main.github_icon_path()
    assert path and os.path.isfile(path)
    assert path.endswith(".svg")
    img = main.github_image()
    assert img is not None


def test_github_icon_fallback(monkeypatch):
    """Missing bundled mark → system icon, never a blank/broken image."""
    monkeypatch.setattr(main, "github_icon_path", lambda: "")
    img = main.github_image()
    assert img is not None
    assert img.get_icon_name() == main.GITHUB_ICON_FALLBACK


def test_no_creator_attribution_in_sources():
    import pathlib
    roots = [pathlib.Path(main.__file__).parent,
             pathlib.Path(main.__file__).parent.parent / "data"]
    banned = ["created by zang", "zang aka", "aka gaurav-x111"]
    for root in roots:
        for p in root.rglob("*.py"):
            text = p.read_text().lower()
            for b in banned:
                assert b not in text, f"{p}: {b}"
        for p in root.rglob("*.xml"):
            text = p.read_text().lower()
            for b in banned:
                assert b not in text, f"{p}: {b}"
    # TTS fallback sample name must be neutral, never a personal name
    src = pathlib.Path(main.__file__).read_text()
    assert 'sample = "Zang"' not in src
