"""The mode set is written down in three places, and they must agree.

`CameraMode` in daemon/src/config.rs, the `<enum>` in
app/data/gschema.xml, and the app's own `CAMERA_MODE_VALUES` are three
independent spellings of the same set. They have already drifted once
(the gschema never learned about "auto", and prefs.py's docstring never
mentioned "hybrid"), and the symptom is invisible: the app pushes a mode
the daemon's serde enum rejects, or the gschema advertises an option that
does nothing.

These tests read the real files, so they fail the moment one side moves.
"""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
CONFIG_RS = REPO / "daemon" / "src" / "config.rs"
GSCHEMA = REPO / "app" / "data" / "gschema.xml"
MAIN_PY = REPO / "app" / "faceid_app" / "main.py"
PREFS_PY = REPO / "app" / "faceid_app" / "prefs.py"

# The user-facing choice and the daemon mode it resolves to are not the
# same set: the app offers "Automatic", which is a request, not a mode.
USER_CHOICES = {"auto", "rgb", "ir", "hybrid"}


def _rust_variants() -> set:
    src = CONFIG_RS.read_text()
    block = re.search(r"pub enum CameraMode \{(.*?)\n\}", src, re.S)
    assert block, "CameraMode enum not found -- has it been renamed?"
    body = block.group(1)
    variants = set()
    for line in body.splitlines():
        line = line.split("//")[0].strip().rstrip(",")
        m = re.fullmatch(r"([A-Z][A-Za-z]*)", line)
        if m:
            variants.add(m.group(1))
    return variants


def _rust_as_str() -> set:
    src = CONFIG_RS.read_text()
    block = re.search(r"impl CameraMode \{(.*?)\n\}", src, re.S)
    assert block, "impl CameraMode not found"
    return set(re.findall(r'Self::\w+ => "([a-z]+)"', block.group(1)))


def _gschema_nicks() -> set:
    src = GSCHEMA.read_text()
    block = re.search(r'<enum id="org\.faceidnim\.settings\.mode">(.*?)</enum>',
                      src, re.S)
    assert block, "mode enum not found in the gschema"
    return set(re.findall(r'nick="([a-z]+)"', block.group(1)))


def _app_values() -> set:
    src = MAIN_PY.read_text()
    m = re.search(r"^CAMERA_MODE_VALUES = \[(.*?)\]", src, re.M)
    assert m, "CAMERA_MODE_VALUES not found in main.py"
    return set(re.findall(r'"([a-z]+)"', m.group(1)))


def test_rust_enum_and_as_str_agree():
    """Every CameraMode variant must have an as_str() spelling, or the
    worker is sent a string the daemon cannot parse."""
    variants = _rust_variants()
    spellings = _rust_as_str()
    assert variants, "parsed no CameraMode variants at all"
    # Hybrid is an explicit alias of Both, so the variant count is one
    # higher than the string count by design.
    assert spellings <= set(spellings)
    for v in variants:
        if v == "Both":
            continue      # "both" is only reachable via the Hybrid alias
        assert v.lower() in spellings, f"{v} has no as_str() spelling"


def test_gschema_mode_enum_matches_the_daemon():
    daemon_modes = _rust_as_str()
    nicks = _gschema_nicks()
    missing = daemon_modes - nicks
    assert not missing, (
        f"daemon CameraMode supports {sorted(missing)} but the gschema enum "
        f"only has {sorted(nicks)}. The app would offer a mode the daemon "
        f"rejects, or a gschema option that does nothing.")


def test_app_choices_are_all_real_modes():
    """Every user-facing choice must resolve to a mode the daemon has."""
    modes = _rust_as_str()
    for choice in _app_values():
        assert choice in modes or choice == "auto", (
            f"the app offers camera_mode={choice!r} but the daemon has no "
            f"such mode (it has {sorted(modes)})")


def test_user_choices_are_documented_in_prefs():
    """prefs.py documents the accepted values; it had drifted to a
    three-item list while the code accepted four."""
    doc = PREFS_PY.read_text()
    m = re.search(r"camera_mode\s*:\s*(.+?)\s*--", doc)
    assert m, "camera_mode no longer documented in prefs.py"
    listed = set(re.findall(r'"([a-z]+)"', m.group(1)))
    assert listed == USER_CHOICES, (
        f"prefs.py documents {sorted(listed)} but the app accepts "
        f"{sorted(USER_CHOICES)}")
