"""Every module in the package must at least compile, and be importable.

This file exists because of a specific, silent failure. `models.py` had
a one-character typo -- `license: str,` instead of `license: str` -- which
made it a SyntaxError. No test imported it, because the tests all inject
fakes, so the entire suite stayed green while `build_engine()` -- the one
function that constructs a real worker -- could not run at all. A worker
that cannot start is not a test failure; it is a machine with no face
unlock.

So: compile every module, and import the model plumbing for real. Cheap,
and it makes "the file has a syntax error" impossible to ship again.
"""
import compileall
import importlib
import pathlib
import py_compile

import pytest

import faceid_vision

PKG_DIR = pathlib.Path(faceid_vision.__file__).parent
APP_DIR = PKG_DIR.parent.parent / "app" / "faceid_app"


def test_every_module_compiles():
    """Byte-compile the whole package. A SyntaxError anywhere is a
    build-breaking bug that no other test would catch."""
    ok = compileall.compile_dir(
        str(PKG_DIR), quiet=2, force=True, legacy=False)
    assert ok, f"a module in {PKG_DIR} failed to compile"


def test_no_stale_pyc_masks_a_broken_source(tmp_path):
    """Guard the guard: py_compile must read the source, not a cached
    .pyc, or this file would pass on a stale tree.

    Writes to tmp_path on purpose. A .pyc dropped into the package
    directory is a build artifact that `cp -r vision` would then ship
    inside the .deb -- and the whole point of this file is to not leave
    stray state where a packaging step can pick it up.
    """
    src = PKG_DIR / "models.py"
    assert src.exists()
    out = tmp_path / "probe.pyc"
    py_compile.compile(str(src), cfile=str(out), doraise=True)
    assert out.exists()


@pytest.mark.parametrize("name", [
    "faceid_vision.models",
    "faceid_vision.hardware",
    "faceid_vision.rt",
    "faceid_vision.landmarks",
    "faceid_vision.scan",
    "faceid_vision.protocol",
    "faceid_vision.camera",
    "faceid_vision.embed",
    "faceid_vision.detect",
    "faceid_vision.fuse",
    "faceid_vision.matching",
    "faceid_vision.quality",
    "faceid_vision.align",
    "faceid_vision.camera",
    "faceid_vision.__main__",
])
def test_module_imports(name):
    """Import for real. A module that only compiles but explodes on
    import is the same class of bug."""
    importlib.import_module(name)


def test_appside_modules_compile_when_present():
    """The settings app ships in the same package; the daemon-side tests
    never import it, so give it the same net."""
    if not APP_DIR.is_dir():
        pytest.skip("app/ not next to the vision package (installed layout)")
    ok = compileall.compile_dir(str(APP_DIR), quiet=2, force=True)
    assert ok, f"a module in {APP_DIR} failed to compile"
