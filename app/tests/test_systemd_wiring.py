"""systemd wiring: drop-in syscall groups must exist, and reinstalls
must not strip manually-enabled units.

1. gpu-devices writes SystemCallFilter group names; unknown names are
   silently ignored by systemd (journal: "Unknown system call group"),
   leaving the sandbox weaker than intended with only a log line.
2. debhelper's was-enabled record goes stale when units are enabled by
   hand: the next reinstall takes the was-enabled=false branch and
   strips the symlinks again (live: units end up disabled after every
   reinstall). sync-systemd-state re-syncs the record with reality.
"""
import os
import pathlib
import re
import stat
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[2]
GPU = REPO / "packaging" / "scripts" / "gpu-devices"
SYNC = REPO / "packaging" / "scripts" / "sync-systemd-state"


def _valid_groups() -> set:
    out = subprocess.run(["systemd-analyze", "syscall-filter"],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return set(re.findall(r"@[\w-]+", out.stdout))


def test_gpu_dropin_groups_exist():
    src = GPU.read_text()
    m = re.search(r"^SYSCALLS='([^']*)'", src, re.M)
    assert m, "SYSCALLS line not found"
    groups = [w for w in m.group(1).split() if w.startswith("@")]
    assert groups, "no syscall groups in drop-in template"
    valid = _valid_groups()
    for g in groups:
        assert g in valid, f"unknown syscall group {g} (systemd ignores it)"


def test_gpu_generator_writes_valid_dropin(tmp_path):
    """Functional run with redirected dirs: the generated conf must
    verify cleanly (no 'Unknown system call group' diagnostics)."""
    conf = tmp_path / "conf"
    drop = tmp_path / "drop"
    state = tmp_path / "state"
    (tmp_path / "dev").mkdir()
    env = dict(os.environ, CONF_DIR=str(conf), DROPIN_DIR=str(drop),
               STATE_FILE=str(state), FAKE_ACCEL="1",
               PATH=f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    (tmp_path / "systemctl").write_text("#!/bin/sh\nexit 0\n")
    (tmp_path / "systemctl").chmod(0o755)
    out = subprocess.run(["sh", str(GPU)], capture_output=True, text=True,
                         env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    conf_file = drop / "10-gpu-devices.conf"
    assert conf_file.is_file(), out.stdout
    out = subprocess.run(["systemd-analyze", "verify", str(conf_file)],
                         capture_output=True, text=True, timeout=60)
    assert "Unknown system call group" not in out.stderr, out.stderr


def _run_sync(active_units, tmp_path, helper_args):
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
        '    for u in ' + " ".join(active_units) + " ; do\n"
        '        [ "$2" = "$u" ] || [ "$3" = "$u" ] && exit 0\n'
        "    done\n"
        "    exit 1\n"
        "fi\n"
        "exit 0\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    deb = d / "deb-systemd-helper"
    deb.write_text("#!/bin/sh\n"
                   f"echo \"helper $*\" >> \"{log}\"\nexit 0\n")
    deb.chmod(deb.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, PATH=f"{d}{os.pathsep}{os.environ['PATH']}")
    out = subprocess.run(["sh", str(SYNC)] + helper_args,
                         capture_output=True, text=True, env=env, timeout=30)
    assert out.returncode == 0, out.stderr
    return log.read_text().splitlines() if log.exists() else []


def test_sync_records_enabled_units(tmp_path):
    calls = _run_sync(["faceid-nimd.service"], tmp_path,
                      ["faceid-nimd.service", "faceid-vision.service"])
    assert "helper enable faceid-nimd.service" in calls
    assert not any("faceid-vision" in c and "helper" in c for c in calls)


def test_sync_leaves_disabled_units_alone(tmp_path):
    calls = _run_sync([], tmp_path,
                      ["faceid-nimd.service", "faceid-vision.service"])
    assert not any(c.startswith("helper") for c in calls)


def test_postinst_calls_sync_helper():
    text = (REPO / "packaging" / "debian" / "postinst").read_text()
    assert "sync-systemd-state" in text
