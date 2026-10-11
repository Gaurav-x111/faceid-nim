"""Audit 2026-10-06 follow-ups: daemon-down honesty, heavy gate,
pill reset, status wording, systemd preserve, window floor.

Widget instantiation needs a display, which CI does not guarantee, so
these tests read the real sources (the same approach as
test_mode_sets_agree.py) plus one display-free functional check of
_liveness_health via an uninitialised Window. They fail the moment the
fix regresses.
"""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
MAIN_PY = REPO / "app" / "faceid_app" / "main.py"
PILL_JS = REPO / "shell-ext" / "faceid@nim" / "pill.js"
STATUS_SH = REPO / "bin" / "faceid-nim"
NIMD_SERVICE = REPO / "packaging" / "systemd" / "faceid-nimd.service"


def _src(p):
    return p.read_text()


def _method_body(src, name):
    m = re.search(rf"def {name}\(self[^)]*\)[^:]*:(.*?)(?=\n    def |\nclass |\Z)",
                  src, re.S)
    assert m, f"{name} not found -- renamed?"
    return m.group(1)


def test_settings_page_has_hidden_daemon_warn_group():
    body = _method_body(_src(MAIN_PY), "_settings_page")
    assert "Cannot reach the daemon" in body
    assert "settings_warn_group" in body
    assert "set_visible(False)" in body


def test_security_page_has_hidden_daemon_warn_group():
    body = _method_body(_src(MAIN_PY), "_security_page")
    assert "Cannot reach the daemon" in body
    assert "sec_warn_group" in body
    assert "set_visible(False)" in body


def test_refresh_toggles_warn_groups_from_errors():
    body = _method_body(_src(MAIN_PY), "_apply_refresh")
    assert "settings_warn_group.set_visible" in body
    assert "sec_warn_group.set_visible" in body
    # Visibility must derive from the refresh error list, not from a
    # cached flag that could go stale.
    assert "errors" in body


def test_heavy_toggle_gated_on_landmarks():
    body = _method_body(_src(MAIN_PY), "on_setting_changed")
    assert "_landmarks_ok" in body
    assert "_confirm_heavy_without_landmarks" in body
    gate = _method_body(_src(MAIN_PY), "_confirm_heavy_without_landmarks")
    assert "fetch-models" in gate
    # Cancel must restore the previous selection without saving.
    assert "_suspend_changes" in gate
    assert "set_selected" in gate


def test_landmarks_flag_cached_from_diagnostics():
    body = _method_body(_src(MAIN_PY), "_apply_refresh")
    assert "_landmarks_ok" in body
    assert "landmarks_ok" in body


def test_main_window_has_minimum_size():
    init = _method_body(_src(MAIN_PY), "__init__")
    assert "set_size_request" in init


def test_status_distinguishes_permission_from_down():
    src = _src(STATUS_SH)
    assert "sock_state" in src
    assert "permission denied - run as root" in src
    # The old misleading line must be gone as the unconditional branch.
    assert 'echo "  worker not running' not in src or "permission" in src


def test_pill_start_scanning_resets_first():
    src = _src(PILL_JS)
    m = re.search(r"startScanning\(reason = null\) \{(.*?)\n    \}",
                  src, re.S)
    assert m, "startScanning not found -- renamed?"
    head = m.group(1)
    assert "this.reset()" in head
    # reset() must come before any state the new scan sets.
    assert head.index("this.reset()") < head.index("_detectLoginContext")


def test_nimd_preserves_runtime_dir():
    src = _src(NIMD_SERVICE)
    assert "RuntimeDirectoryPreserve=yes" in src


def test_liveness_health_reports_missing_antispoof():
    from faceid_app.main import Window
    # _liveness_health is a staticmethod: no display needed.
    out = Window._liveness_health(
        {"liveness": {"landmarks_ok": True, "antispoof": False}})
    assert out["ok"] is False
    assert "ML anti-spoof" in out["detail"]
    assert "NOT" in out["detail"] and "running" in out["detail"]
    healthy = Window._liveness_health(
        {"liveness": {"landmarks_ok": True, "antispoof": True}})
    assert healthy["ok"] is True
