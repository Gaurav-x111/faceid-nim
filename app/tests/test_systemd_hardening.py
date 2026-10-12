"""H10: incremental systemd hardening is present and parses.

Source-only: asserts the packaged units carry each knob and that
systemd-analyze verify accepts them (skipped when systemd-analyze is
absent). Never touches live units.
"""
import pathlib
import shutil
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[2]
DAEMON = REPO / "packaging" / "systemd" / "faceid-nimd.service"
WORKER = REPO / "packaging" / "systemd" / "faceid-vision.service"

REQUIRED = (
    "ProtectClock=yes",
    "ProtectHostname=yes",
    "ProtectKernelLogs=yes",
    "RestrictAddressFamilies=AF_UNIX AF_NETLINK",
    "SystemCallArchitectures=native",
)

# Pre-existing baseline that must not regress.
BASELINE = (
    "ProtectSystem=strict",
    "NoNewPrivileges=yes",
    "PrivateTmp=yes",
    "ProtectKernelTunables=yes",
    "SystemCallFilter=@system-service",
)


def test_hardening_knobs_present():
    for unit in (DAEMON, WORKER):
        text = unit.read_text()
        for knob in REQUIRED + BASELINE:
            assert knob in text, f"{unit.name}: missing {knob}"


def test_worker_keeps_device_and_user_confinement():
    text = WORKER.read_text()
    assert "User=faceid" in text
    assert "DevicePolicy=closed" in text
    assert "CapabilityBoundingSet=" in text


def test_units_verify_clean():
    if shutil.which("systemd-analyze") is None:
        import pytest
        pytest.skip("systemd-analyze not installed")
    for unit in (DAEMON, WORKER):
        proc = subprocess.run(
            ["systemd-analyze", "verify", str(unit)],
            capture_output=True, text=True, timeout=60)
        errors = [l for l in (proc.stderr or "").splitlines()
                  if "Failed" in l or "Unknown" in l or "Invalid" in l]
        assert not errors, f"{unit.name}: {proc.stderr[:2000]}"
