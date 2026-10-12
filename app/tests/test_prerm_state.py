"""prerm disables units on remove/purge/deconfigure (binaries go away;
a running daemon with deleted files is worse). But a reinstall also
passes through prerm, so blind disabling leaves units off forever:
postinst's upgrade path only restarts *running* units and debhelper's
was-enabled record is stale-false. The fix: prerm records which units
were enabled; postinst restores exactly those on configure.

Tests drive the two helpers with stubbed systemctl under a temp
RUNDIR; nothing touches the live system.
"""
import os
import pathlib
import stat
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[2]
RECORD = REPO / "packaging" / "scripts" / "record-unit-state"
RESTORE = REPO / "packaging" / "scripts" / "restore-unit-state"


def _stubs(tmp_path, enabled_units, fail_enable=()):
    d = tmp_path / "stubs"
    d.mkdir(parents=True, exist_ok=True)
    log = d / "calls.log"
    if log.exists():
        log.unlink()
    fake = d / "systemctl"
    fake.write_text(
        "#!/bin/sh\n"
        f"echo \"$*\" >> \"{log}\"\n"
        'if [ "$1" = "is-enabled" ]; then\n'
        '    for u in ' + " ".join(enabled_units) + " ; do\n"
        '        [ "$2" = "$u" ] || [ "$3" = "$u" ] && exit 0\n'
        "    done\n"
        "    exit 1\n"
        "fi\n"
        'if [ "$1" = "enable" ]; then\n'
        '    for u in ' + " ".join(fail_enable) + " ; do\n"
        '        [ "$3" = "$u" ] && exit 1\n'
        "    done\n"
        "fi\n"
        "exit 0\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    rundir = tmp_path / "run"
    rundir.mkdir(parents=True, exist_ok=True)
    log.write_text("")
    env = dict(os.environ, PATH=f"{d}{os.pathsep}{os.environ['PATH']}",
               RUNDIR=str(rundir))
    return env, rundir, log


def _run(helper, units, env):
    out = subprocess.run(["sh", str(helper)] + units, capture_output=True,
                         text=True, env=env, timeout=30)
    assert out.returncode == 0, out.stderr


def test_record_flags_enabled_units_only(tmp_path):
    env, rundir, _log = _stubs(tmp_path, ["faceid-nimd.service"])
    _run(RECORD, ["faceid-nimd.service", "faceid-vision.service"], env)
    assert (rundir / "was-enabled-faceid-nimd.service").is_file()
    assert not (rundir / "was-enabled-faceid-vision.service").exists()


def test_restore_enables_flagged_units(tmp_path):
    env, rundir, log = _stubs(tmp_path, [])
    (rundir / "was-enabled-faceid-nimd.service").write_text("")
    (rundir / "was-enabled-faceid-vision.service").write_text("")
    _run(RESTORE, ["faceid-nimd.service", "faceid-vision.service"], env)
    calls = log.read_text()
    assert "enable --now faceid-nimd.service" in calls
    assert "enable --now faceid-vision.service" in calls
    assert not (rundir / "was-enabled-faceid-nimd.service").exists()
    assert not (rundir / "was-enabled-faceid-vision.service").exists()


def test_restore_ignores_unflagged_units(tmp_path):
    env, _rundir, log = _stubs(tmp_path, [])
    _run(RESTORE, ["faceid-nimd.service"], env)
    assert "enable --now" not in log.read_text()


def test_prerm_and_postinst_wire_helpers():
    prerm = (REPO / "packaging" / "debian" / "prerm").read_text()
    assert "record-unit-state" in prerm
    postinst = (REPO / "packaging" / "debian" / "postinst").read_text()
    assert "restore-unit-state" in postinst
    for p in (RECORD, RESTORE):
        out = subprocess.run(["sh", "-n", str(p)], capture_output=True,
                             text=True, timeout=30)
        assert out.returncode == 0, out.stderr
