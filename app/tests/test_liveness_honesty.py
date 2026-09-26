"""The app must never imply a check that is not running.

The daemon's Diagnostics payload grew a `liveness` block reporting what
the worker can actually do, with a reason for anything missing. The rule
this file enforces: when a liveness backend is absent, the UI says so in
the status line and in a toast. It does not show a healthy green state,
and it does not name a check that never ran.

`_liveness_health` and `_diag_value` are pure functions on the settings
window class, so they are tested here directly rather than through GTK.
"""
import ast
import pathlib

import pytest

_APP = pathlib.Path(__file__).resolve().parent.parent / "faceid_app" / "main.py"


def _load_pure_helpers():
    """Extract the two pure helpers from main.py without importing GTK.

    main.py calls gi.require_version at import time, which is not
    available in a headless test run, and the app must not gain a
    hard GTK dependency just to be testable.
    """
    src = _APP.read_text()
    tree = ast.parse(src)
    wanted = ("_liveness_health", "_diag_value")
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            node.decorator_list = []
            found[node.name] = ast.get_source_segment(src, node)
    missing = [n for n in wanted if n not in found]
    assert not missing, f"main.py no longer defines {missing}"
    ns: dict = {}
    exec("\n\n".join(found[n] for n in wanted), ns)   # noqa: S102
    return ns


_NS = _load_pure_helpers()
health = _NS["_liveness_health"]
diag_value = _NS["_diag_value"]


def test_the_helpers_load_without_gtk():
    """Guard the guard: the extraction must not have started pulling GTK
    in, or these tests would need a display. Asserted by the helpers
    being callable and by gi not being required."""
    assert callable(health)
    assert callable(diag_value)
    assert not any(str(k).startswith("gi") for k in _NS), \
        "a GTK symbol leaked into the extracted namespace"


# ---- healthy install --------------------------------------------------

def test_fully_healthy_install_reports_nothing_wrong():
    d = {"liveness": {"landmarks": "mediapipe_tasks", "landmarks_ok": True,
                      "antispoof": True}}
    out = health(d)
    assert out["ok"] is True
    assert out["summary"] == ""
    assert out["detail"] == "", "a healthy machine must not raise a toast"


def test_absent_liveness_block_is_not_treated_as_unhealthy():
    """No worker reply means "not known", not "broken". A daemon that is
    merely old must not produce a false alarm."""
    assert health({})["ok"] is True
    assert health({"liveness": None})["ok"] is True


# ---- degraded installs ------------------------------------------------

def test_missing_landmarks_is_named_specifically():
    d = {"liveness": {
        "landmarks": "none", "landmarks_ok": False,
        "landmarks_reason": "landmark model not installed",
        "antispoof": True}}
    out = health(d)
    assert out["ok"] is False
    assert "blink" in out["summary"]
    assert "attention" in out["summary"]
    assert "ML anti-spoof" not in out["summary"], \
        "anti-spoof IS running here and must not be listed as lost"
    assert "NOT running" in out["detail"]
    assert "fetch-models" in out["detail"], "must say how to fix it"


def test_missing_antispoof_is_named_specifically():
    d = {"liveness": {"landmarks_ok": True, "antispoof": False,
                      "antispoof_reason": "no anti-spoof model installed"}}
    out = health(d)
    assert out["ok"] is False
    assert "ML anti-spoof" in out["summary"]
    assert "blink" not in out["summary"]


def test_both_missing_names_both():
    d = {"liveness": {"landmarks_ok": False, "antispoof": False}}
    out = health(d)
    assert out["ok"] is False
    assert "blink" in out["summary"] and "ML anti-spoof" in out["summary"]


def test_the_heavy_strictness_consequence_is_stated():
    """The user-facing consequence matters more than the mechanism: with
    no landmarks, Heavy strictness can never be satisfied, and that is a
    lockout-shaped problem they must be warned about."""
    d = {"liveness": {"landmarks_ok": False, "antispoof": True}}
    assert "Heavy" in health(d)["detail"]


# ---- diagnostics rendering -------------------------------------------

def test_nested_dicts_render_readably_not_as_a_repr():
    v = {"providers": ["CUDAExecutionProvider", "CPUExecutionProvider"],
         "summary": "CUDA · 16 threads", "unverified": False}
    out = diag_value(v)
    assert out
    assert "{" not in out and "}" not in out, "raw dict repr leaked into the UI"
    assert "CUDAExecutionProvider" in out


def test_diag_value_scalars_and_empties():
    assert diag_value(None) == "not set"
    assert diag_value(True) == "yes"
    assert diag_value(False) == "no"
    assert diag_value(0.42) == "0.42"
    assert diag_value([]) == "none"
    assert diag_value({}) == "unknown"


def test_diag_titles_exist_for_the_new_keys():
    from faceid_app.prefs import DIAG_TITLES

    for key in ("models_ready", "liveness", "accel", "worker"):
        assert key in DIAG_TITLES, f"{key} would render as a raw key name"
        assert DIAG_TITLES[key] == DIAG_TITLES[key].strip()
        assert DIAG_TITLES[key][0].isupper()


def test_unknown_keys_still_get_a_readable_title():
    from faceid_app.prefs import diag_title

    assert diag_title("some_future_field") == "Some future field"
    # An empty Adw row title renders as a visibly broken row.
    assert diag_title("") == "Unknown setting"
    assert diag_title(None)


@pytest.mark.parametrize("key", ["models_ready", "liveness", "accel"])
def test_diag_title_never_leaks_a_snake_case_name(key):
    from faceid_app.prefs import diag_title

    assert "_" not in diag_title(key)
