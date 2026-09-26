"""Hardware camera discovery -- re-exported from the vision package.

This module used to hold the implementation. It now lives in
``faceid_vision.cameras`` because headless provisioning needs it too: the
daemon's first-start auto-configuration, `faceid-nim status` and
`faceid-nim diagnose` all run without the GTK app, and none of them can
import anything out of it.

Two implementations would eventually disagree about which node is the IR
sensor, and the result would be a UI that says one thing while the daemon
uses another. So there is one implementation and this file only points at
it. Existing imports keep working unchanged.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


def _by_path_guesses(here: Path) -> list[Path]:
    """Locations the by-path last resort will try, most specific first.

    Kept as a function so it can be asserted on directly: these paths are
    the ones no sys.path root covers, which is exactly why they exist.
    """
    return [
        here.parent.parent / "vision" / "faceid_vision" / "cameras.py",
        Path("/usr/libexec/faceid-nim/vision/faceid_vision/cameras.py"),
        here / "cameras.py",
    ]


def _load() -> object:
    """Import faceid_vision.cameras, vendored or installed.

    The app runs on the *system* python (the bundled venv is worker-only
    and has no PyGObject), so in an installed layout the vision package
    is not importable at all: it lives inside
    /usr/libexec/faceid-nim/venv/lib/pythonX.Y/site-packages, which the
    system interpreter does not search. A source checkout has it as a
    sibling directory instead. Try the normal import, then both layouts.

    Note that a missing __init__.py does not stop the sys.path import:
    Python 3 treats such a directory as a namespace package. So the
    by-path loader below really is a last resort, and only the
    _by_path_guesses locations can ever reach it.
    """
    try:
        from faceid_vision import cameras  # type: ignore

        return cameras
    except ImportError:
        pass

    here = Path(__file__).resolve().parent
    roots = [
        Path("/usr/libexec/faceid-nim/venv/lib"),   # installed .deb
        here.parent.parent / "vision",             # source checkout
        here.parent,                               # vendored alongside
        Path("/usr/libexec/faceid-nim/vision"),    # by-path roots
    ]
    # Overridable so the layout resolution can be tested for real instead
    # of being asserted by reading the code. Colon-separated, like PATH.
    override = os.environ.get("FACEID_VISION_ROOTS")
    if override:
        roots = [Path(p) for p in override.split(os.pathsep) if p]

    for base in roots:
        if not base.is_dir():
            continue
        # site-packages for a venv, and the package directory itself for
        # a source tree. Both spellings, because guessing wrong here
        # means the settings app cannot show the user their cameras.
        candidates = [base, *sorted(base.glob("python*/site-packages"))]
        for path in candidates:
            if not path.is_dir():
                continue
            text = str(path)
            if text not in sys.path:
                sys.path.append(text)
            try:
                from faceid_vision import cameras  # type: ignore

                return cameras
            except ImportError:
                continue

    # Last resort: load the file directly by path. Keeps the settings app
    # usable (it can still show the user what it found) even when the
    # vision package is missing entirely, which is exactly when the user
    # most needs to be told something.
    for guess in _by_path_guesses(here):
        if guess.is_file():
            name = "faceid_vision_cameras_fallback"
            spec = importlib.util.spec_from_file_location(name, guess)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                # Register BEFORE executing: @dataclass resolves the
                # module's __dict__ through sys.modules while the class
                # body is being created, and raises
                # "AttributeError: 'NoneType' object has no attribute
                # '__dict__'" if it is not there yet.
                sys.modules[name] = mod
                try:
                    spec.loader.exec_module(mod)
                except Exception:
                    sys.modules.pop(name, None)
                    raise
                return mod
    raise ImportError(
        "faceid_vision.cameras not found; camera discovery is unavailable")


_impl = _load()

# Re-export the public surface. Explicit names, not a glob, so a typo here
# is an ImportError at start-up rather than an AttributeError later.
CameraInfo = _impl.CameraInfo
CameraFormat = _impl.CameraFormat
MONO_FIVECC = _impl.MONO_FIVECC
IR_NAME_HINTS = _impl.IR_NAME_HINTS
KW_RGB = _impl.KW_RGB
fivecc = _impl.fivecc
fivecc_to_u32 = _impl.fivecc_to_u32
probe = _impl.probe
discover = _impl.discover
pick = _impl.pick
resolve_plan = _impl.resolve_plan
auto_config = _impl.auto_config
summary = _impl.summary
sample_light = _impl.sample_light
light_label = _impl.light_label

__all__ = [
    "CameraInfo", "CameraFormat", "MONO_FIVECC", "IR_NAME_HINTS", "KW_RGB",
    "fivecc", "fivecc_to_u32", "probe", "discover", "pick", "resolve_plan",
    "auto_config", "summary", "sample_light", "light_label",
]


if __name__ == "__main__":
    import sys as _sys

    targets = _sys.argv[1:] or None
    for c in discover():
        if targets and c.path not in targets:
            continue
        flag = "BUSY" if c.busy else ("OK" if c.readable else "NOACC")
        print(f"{c.path} [{flag}] kind={c.kind!r} {summary(c)}")
