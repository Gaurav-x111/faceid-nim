"""Unit tests for ir_emitter detection (no hardware needed)."""
from faceid_vision import ir_emitter


def test_status_returns_hint(monkeypatch):
    monkeypatch.setattr(ir_emitter.shutil, "which", lambda *_a, **_k: None)
    s = ir_emitter.status()
    assert s.tool_present is False
    assert "passive IR" in s.hint


def test_status_tool_present(monkeypatch):
    monkeypatch.setattr(ir_emitter.shutil, "which", lambda *_a, **_k: "/usr/bin/x")
    s = ir_emitter.status()
    assert s.tool_present is True
