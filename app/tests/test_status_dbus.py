"""faceid-nim status must report D-Bus name ownership.

Regression test: the daemon kept serving PAM without its UI bus name
and status showed three green units, so nothing looked wrong. The
status now carries a D-Bus line that says owned or MISSING (a
problem). busctl is stubbed via PATH; everything else runs for real.
"""
import os
import pathlib
import stat
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[2]
CLI = REPO / "bin" / "faceid-nim"


def _run_status(busctl_body: str) -> str:
    stubs = pathlib.Path(os.environ.get("PYTEST_TMP", "/tmp"))
    d = stubs / f"busctl-stub-{os.getpid()}-{len(busctl_body)}"
    d.mkdir(parents=True, exist_ok=True)
    fake = d / "busctl"
    fake.write_text("#!/bin/sh\n"
                     "if [ \"$1\" = \"--system\" ] && [ \"$2\" = \"list\" ]; then\n"
                     f"    printf '%s' \"{busctl_body}\"\n"
                     "fi\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, PATH=f"{d}{os.pathsep}{os.environ['PATH']}")
    out = subprocess.run(["sh", str(CLI), "status"], capture_output=True,
                         text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_status_shows_dbus_owned():
    body = "org.faceidnim.Daemon1 1234 - - faceid-nimd\\n"
    out = _run_status(body)
    assert "D-Bus name org.faceidnim.Daemon1 : owned" in out


def test_status_shows_dbus_missing_as_problem():
    out = _run_status("org.freedesktop.DBus 1 - - dbus-daemon\\n")
    assert "D-Bus name org.faceidnim.Daemon1 : MISSING" in out
    assert "service unavailable" in out
