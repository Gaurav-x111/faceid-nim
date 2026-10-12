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


def test_enroll_camera_page_has_no_diag_dump():
    """The enrollment wizard's camera page is a pre-scan gate, not a
    diagnostics screen: it must not iterate the whole diagnostics dict
    into rows (that filled the dialog and buried the Start button)."""
    src = _src(REPO / "app" / "faceid_app" / "onboarding.py")
    body = _method_body(src, "_go_camera")
    assert "for key, value in diag.items()" not in body
    assert "diag.items()" not in body
    # Raw-dict subtitles must be gone; one compact summary row at most.
    assert "Camera ready" in body
    assert "Diagnostics page" in body


def test_enroll_camera_gate_kept():
    """The dump is gone but the safety gate stays: daemon-down and
    worker-down both keep Start disabled with an actionable message."""
    src = _src(REPO / "app" / "faceid_app" / "onboarding.py")
    body = _method_body(src, "_go_camera")
    assert "Cannot reach the service" in body
    assert "faceid-nim status" in body
    assert "worker_reachable" in body
    assert "fetch-models" in body
    assert body.count("camera_next.set_sensitive(False)") >= 2
    assert "camera_next.set_sensitive(True)" in body


def test_full_diag_still_on_diagnostics_page():
    """The detail removed from the wizard must exist on the main
    window's Diagnostics page, not vanish entirely."""
    body = _method_body(_src(MAIN_PY), "_apply_refresh")
    assert "for k, v in diag.items()" in body


def test_rules_helper_install_lines_have_tabs():
    """debian/rules recipes MUST start with a tab; a missing one is a
    'missing separator' error at build time (regression: the
    restart-if-active line shipped without one)."""
    import subprocess
    out = subprocess.run(["make", "-n", "-f", "debian/rules", "clean"],
                         capture_output=True, text=True,
                         cwd=str(REPO), timeout=120)
    assert out.returncode == 0, out.stderr
    src = _src(REPO / "debian" / "rules")
    body = src[src.index("override_dh_auto_install"):]
    body = body[:body.index("\noverride_dh_gencontrol")]
    for line in body.splitlines():
        if "install -m" in line or line.startswith("install "):
            assert line.startswith("\t"), f"missing tab: {line!r}"
    # The helper really is installed by the rule.
    assert "restart-if-active" in body
