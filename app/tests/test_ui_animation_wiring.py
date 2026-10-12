"""UI/animation wiring regression tests (no display needed).

Covers Phase 5-6 + Phase 2 state model + preview-startup fix:
- sidebar order matches the required primary structure
- GitHub link constants + open_url abstraction (mocked launcher)
- GitHub icon resolves to the bundled mark (or theme fallback)
- no promotional creator attribution in app sources
- no simulated preview surface (removed; live TestScan instead)
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


def test_no_simulated_preview_surface():
    # The canned scanning/recognized/denied preview layer was removed
    # 2026-10-12: the Appearance page offers a real TestScan instead,
    # so none of this machinery may come back unnoticed.
    for name in ("PREVIEW_STATES", "PREVIEW_STATE_LABELS",
                 "SCAN_STATE_TO_PREVIEW", "canonical_preview_state",
                 "preview_plan_for_state", "cairo_preview_style"):
        assert not hasattr(main, name), name
    for meth in ("_preview_selected_state", "_preview_denied",
                 "_preview_success", "_preview_video_pill",
                 "_preview_opening_inline", "_play_preview_plan",
                 "_close_preview", "_preview_plan_widget",
                 "_draw_opening_preview", "_draw_opening_preview_at",
                 "_on_preview_state_changed", "_selected_preview_state",
                 "_find_video_pill_harness", "_daemon_error_dialog"):
        assert not hasattr(main.Window, meth), meth


def test_try_it_row_runs_real_test_scan():
    src = inspect.getsource(main.Window._appearance_groups)
    assert "Try it live" in src
    assert "self.on_test" in src
    assert "Preview" not in src


def test_scan_face_values():
    assert main.SCAN_FACE_VALUES == ["iphone", "apple", "arena", "classic"]
    assert main.SCAN_FACE_LABELS == ["iPhone (default)", "Smile", "Orbit", "Classic"]
    assert len(main.SCAN_FACE_LABELS) == len(main.SCAN_FACE_VALUES)
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


def test_overview_has_no_next_step():
    """The dead 'Next step' block is gone: its button did nothing."""
    import pathlib
    src = pathlib.Path(main.__file__).read_text()
    assert "Next step" not in src
    assert "overview_next" not in src
    assert "_on_overview_next" not in src


def test_sidebar_footer_is_github_only():
    """No faceid-nim footer text under the sidebar; GitHub row keeps
    its label, tooltip and browser wiring."""
    import pathlib
    src = pathlib.Path(main.__file__).read_text()
    assert 'faceid-nim{v}' not in src
    assert "GitHub ↗" in src
    assert "GITHUB_TOOLTIP" in src
    body = src[src.index("def _on_github_nav"):]
    assert "open_url(GITHUB_URL)" in body
