"""Regression tests for the --no-verify gate (Phase 2 Fix 4).

--no-verify must be refused unless FACEID_DEV=1, and loud when used.
The gate sits before build_engine, so these tests run with no models
and no camera (build_engine is stubbed).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import apcer_bpcer


class Sentinel(Exception):
    pass


def _argv(*extra):
    return ["apcer_bpcer.py", "--root", "/nonexistent-eval-root", *extra]


def test_no_verify_refused_without_dev_env(monkeypatch, capsys):
    monkeypatch.delenv("FACEID_DEV", raising=False)
    monkeypatch.setattr(sys, "argv", _argv("--no-verify"))
    rc = apcer_bpcer.main()
    assert rc == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err


def test_no_verify_allowed_with_dev_env_but_loud(monkeypatch, capsys):
    monkeypatch.setenv("FACEID_DEV", "1")
    monkeypatch.setattr(sys, "argv", _argv("--no-verify"))
    # Gate passes; the stubbed engine then blows up -- the point is we
    # got PAST the gate (no REFUSED) with a loud WARNING.
    def boom(**kwargs):
        raise Sentinel()
    monkeypatch.setattr(apcer_bpcer, "build_engine", boom)
    with pytest.raises(Sentinel):
        apcer_bpcer.main()
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "REFUSED" not in err


def test_verify_path_unaffected(monkeypatch):
    # No --no-verify, no env: straight to the engine stub.
    monkeypatch.delenv("FACEID_DEV", raising=False)
    monkeypatch.setattr(sys, "argv", _argv())
    def boom(**kwargs):
        raise Sentinel()
    monkeypatch.setattr(apcer_bpcer, "build_engine", boom)
    with pytest.raises(Sentinel):
        apcer_bpcer.main()
