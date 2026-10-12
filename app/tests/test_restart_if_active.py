"""restart-if-active restarts only running units, never starts others.

Upgrade path: the old daemon keeps serving after an upgrade unless it
is restarted; a stopped unit must stay stopped, and nothing here may
enable face unlock (liveness only, no config/PAM/enabled-flag touch).
systemctl is stubbed via PATH and its calls are recorded.
"""
import os
import pathlib
import stat
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[2]
HELPER = REPO / "packaging" / "scripts" / "restart-if-active"


def _run_helper(active_units):
    calls = []
    d = pathlib.Path(f"/tmp/restart-stub-{os.getpid()}")
    d.mkdir(parents=True, exist_ok=True)
    log = d / "calls.log"
    if log.exists():
        log.unlink()
    fake = d / "systemctl"
    fake.write_text(
        "#!/bin/sh\n"
        f"echo \"$*\" >> \"{log}\"\n"
        'if [ "$1" = "is-active" ]; then\n'
        '    for u in ' + " ".join(active_units) + " ; do\n"
        '        [ "$3" = "$u" ] && exit 0\n'
        "    done\n"
        "    exit 1\n"
        "fi\n"
        "exit 0\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, PATH=f"{d}{os.pathsep}{os.environ['PATH']}")
    out = subprocess.run(["sh", str(HELPER), "faceid-nimd.service",
                          "faceid-vision.service"],
                         capture_output=True, text=True, env=env, timeout=30)
    assert out.returncode == 0, out.stderr
    return log.read_text().splitlines() if log.exists() else []


def test_syntax_ok():
    out = subprocess.run(["sh", "-n", str(HELPER)], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr


def test_restarts_running_unit_only():
    calls = _run_helper(["faceid-nimd.service"])
    assert "try-restart faceid-nimd.service" in calls
    assert not any(c.startswith("try-restart faceid-vision") for c in calls)
    assert not any(c.split()[0] in ("start", "enable", "restart")
                   for c in calls), calls


def test_stopped_units_untouched():
    assert _run_helper([]) == [
        "is-active --quiet faceid-nimd.service",
        "is-active --quiet faceid-vision.service",
    ]
