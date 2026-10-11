"""The opening-animation set is written down in three places: the app's
``openings.BUILTIN``, ``shell-ext/faceid@nim/opening.js`` and the
``faceid-nim-dynamic-island`` copy of that file. They have already
drifted once: the dynamic-island copy never learned about the ``glance``
builtin, and its ``load()`` fell back to ``logo`` for every builtin, so
picking Glance (or Glow, or No splash) on the Opening page showed the
App Logo on the lock screen instead.

These tests read the real files, so they fail the moment one side moves.
"""
import pathlib
import re

from faceid_app import openings

REPO = pathlib.Path(__file__).resolve().parents[2]
SHELL_OPENING_JS = REPO / "shell-ext" / "faceid@nim" / "opening.js"
ISLAND_OPENING_JS = (REPO / "faceid-nim-dynamic-island" / "faceid-nim"
                     / "shell-ext" / "faceid@nim" / "opening.js")

JS_FILES = (SHELL_OPENING_JS, ISLAND_OPENING_JS)


def _js_builtins(path: pathlib.Path) -> set:
    src = path.read_text()
    block = re.search(r"const BUILTIN = \{(.*?)\n\};", src, re.S)
    assert block, f"BUILTIN block not found in {path} -- renamed?"
    return set(re.findall(r"^    (\w+): \{", block.group(1), re.M))


def _js_default(path: pathlib.Path) -> str:
    """The id an unknown/empty config resolves to in this file."""
    src = path.read_text()
    fallback = re.search(
        r"const fallback = \{ active: '(\w+)'", src)
    assert fallback, f"fallback default not found in {path}"
    active = re.search(r"active: String\(data\.active \|\| '(\w+)'\)", src)
    assert active, f"active default not found in {path}"
    load = re.search(
        r"\? cfg\.active : '(\w+)';", src)
    assert load, f"load() fallback not found in {path}"
    assert fallback.group(1) == active.group(1) == load.group(1), (
        f"{path} disagrees with itself: fallback={fallback.group(1)!r} "
        f"active={active.group(1)!r} load={load.group(1)!r}")
    return fallback.group(1)


def test_js_builtins_match_the_app():
    """Every builtin the app offers must exist in the lock-screen code,
    or the lock shows a different animation than the one picked."""
    app_ids = set(openings.BUILTIN)
    assert app_ids, "parsed no BUILTIN ids from openings.py at all"
    for path in JS_FILES:
        assert path.is_file(), f"{path} is missing"
        js_ids = _js_builtins(path)
        assert js_ids == app_ids, (
            f"{path} knows builtins {sorted(js_ids)} but the app offers "
            f"{sorted(app_ids)}. A choice with no lock-screen twin "
            f"renders as something else.")


def test_js_fallback_is_the_app_default():
    """An unknown active id must resolve to the app's default, in every
    copy, or a valid pick silently becomes another animation."""
    want = openings.default_config()["active"]
    assert want in openings.BUILTIN, "app default is not a builtin?!"
    for path in JS_FILES:
        assert _js_default(path) == want, (
            f"{path} falls back to {_js_default(path)!r} but the app "
            f"default is {want!r}")
