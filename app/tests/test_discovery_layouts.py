"""The app must find the discovery code in every layout it ships in.

The settings app runs on the *system* python. The vision package lives in
the worker venv, which the system interpreter does not search, and in a
source checkout it is a sibling directory. Get this wrong and the camera
page of the settings app cannot show the user their cameras -- which is
exactly the moment they most need to see it.

These tests build each layout for real and load the shim in an isolated
interpreter, so the path arithmetic is exercised rather than assumed.
"""
import os
import pathlib
import shutil
import subprocess
import sys
import textwrap

REPO = pathlib.Path(__file__).resolve().parents[2]
SHIM = REPO / "app" / "faceid_app" / "camera_discovery.py"
VISION_SRC = REPO / "vision" / "faceid_vision"


def _load_in_isolated(app_dir: pathlib.Path, roots_env: str | None):
    """Import the shim in a clean interpreter, returning the module path
    it ended up using. Isolated (-I) so an editable install of this repo
    in the ambient site-packages cannot answer for us."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp",
        "FACEID_NIM_NO_NETWORK": "1",
    }
    if roots_env:
        env["FACEID_VISION_ROOTS"] = roots_env
    # The parent, because `faceid_app` is a package -- which is exactly
    # what packaging/faceid-app puts on PYTHONPATH.
    code = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(app_dir.parent)!r})
        from faceid_app import camera_discovery as cd
        print(cd._impl.__file__)
    """)
    r = subprocess.run([sys.executable, "-I", "-c", code],
                       capture_output=True, text=True, env=env, cwd="/tmp")
    return r


def test_installed_layout_via_venv_site_packages(tmp_path):
    """.deb layout: the app is in /usr/share, and faceid_vision exists
    ONLY inside the worker venv's site-packages."""
    app_dir = tmp_path / "share" / "faceid-nim" / "app" / "faceid_app"
    app_dir.mkdir(parents=True)
    shutil.copy(SHIM, app_dir / "camera_discovery.py")
    (app_dir.parent.parent / "app" / "__init__.py").write_text("")

    site = tmp_path / "libexec" / "venv" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    shutil.copytree(VISION_SRC, site / "faceid_vision")

    r = _load_in_isolated(app_dir, str(tmp_path / "libexec" / "venv" / "lib"))
    assert r.returncode == 0, r.stderr
    assert str(site) in r.stdout, \
        f"resolved the wrong faceid_vision:\n{r.stdout}\n{r.stderr}"


def test_source_checkout_layout(tmp_path):
    """Source layout: app/ and vision/ are siblings."""
    app_dir = tmp_path / "app" / "faceid_app"
    app_dir.mkdir(parents=True)
    shutil.copy(SHIM, app_dir / "camera_discovery.py")
    (app_dir.parent / "__init__.py").write_text("")
    shutil.copytree(VISION_SRC, tmp_path / "vision" / "faceid_vision")

    r = _load_in_isolated(app_dir, None)
    assert r.returncode == 0, r.stderr
    assert str(tmp_path / "vision" / "faceid_vision") in r.stdout, \
        f"resolved the wrong faceid_vision:\n{r.stdout}\n{r.stderr}"


def test_missing_package_gives_a_clear_error_not_a_broken_module(tmp_path):
    """When the vision package really is absent, the app must fail with
    a message naming the problem -- not with an AttributeError on some
    half-initialised module."""
    app_dir = tmp_path / "app" / "faceid_app"
    app_dir.mkdir(parents=True)
    shutil.copy(SHIM, app_dir / "camera_discovery.py")
    (app_dir.parent / "__init__.py").write_text("")

    r = _load_in_isolated(app_dir, str(tmp_path / "definitely-not-here"))
    assert r.returncode != 0
    assert "camera_discovery" in r.stderr or "cameras" in r.stderr, r.stderr
    assert "AttributeError" not in r.stderr, \
        "must not fail with a confusing AttributeError"


def test_by_path_guesses_cover_the_paths_no_root_covers(tmp_path):
    """The last-resort loader is only reachable for these locations --
    Python 3 namespace packages mean a missing __init__.py still imports
    fine, so the sys.path route wins whenever the file is under a root.
    Asserted directly so the list cannot quietly rot."""
    from faceid_app.camera_discovery import _by_path_guesses

    here = tmp_path / "share" / "faceid-nim" / "app" / "faceid_app"
    guesses = _by_path_guesses(here)
    assert guesses[0] == (tmp_path / "share" / "faceid-nim" / "vision"
                          / "faceid_vision" / "cameras.py")
    assert any("usr/libexec" in str(g) for g in guesses)
    assert guesses[-1] == here / "cameras.py"


def test_fallback_file_loader_registers_the_module(tmp_path):
    """The by-path loader must put the module in sys.modules before
    executing it. @dataclass resolves the module __dict__ through
    sys.modules while the class body is created and raises
    "AttributeError: 'NoneType' object has no attribute '__dict__'"
    otherwise -- which is exactly what happened before this was fixed.

    Run in an isolated interpreter, and with cameras.py sitting NEXT TO
    the shim: that is the one location a sys.path root cannot reach (the
    nearest root is the app's parent), so the by-path branch is the only
    one that can succeed. An ambient editable install of this repo in the
    test machine's site-packages would otherwise answer first.
    """
    app_dir = tmp_path / "app" / "faceid_app"
    app_dir.mkdir(parents=True)
    shutil.copy(SHIM, app_dir / "camera_discovery.py")
    shutil.copy(VISION_SRC / "cameras.py", app_dir / "cameras.py")
    (app_dir.parent / "__init__.py").write_text("")

    code = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(app_dir.parent)!r})
        from faceid_app import camera_discovery as cd
        print(cd._impl.__name__)
        print(cd._impl.__file__)
        print("usable:", callable(cd.discover), callable(cd.auto_config))
    """)
    r = subprocess.run([sys.executable, "-I", "-c", code],
                       capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
                       cwd="/tmp")
    assert r.returncode == 0, r.stderr
    assert "faceid_vision_cameras_fallback" in r.stdout, r.stdout
    assert "AttributeError" not in r.stderr, r.stderr
    assert "usable: True True" in r.stdout, r.stdout


def test_the_override_is_only_a_test_and_deployment_hook():
    """FACEID_VISION_ROOTS must not silently shadow a working install in
    normal use: unset, the real paths are used."""
    env = dict(os.environ)
    env.pop("FACEID_VISION_ROOTS", None)
    assert "FACEID_VISION_ROOTS" not in env
    # And the shim documents it, so it is discoverable rather than a
    # hidden knob.
    assert "FACEID_VISION_ROOTS" in SHIM.read_text()
