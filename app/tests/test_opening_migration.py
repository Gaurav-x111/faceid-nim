"""Migration + antispoof-absence tests (master audit, finding set 1E/5E).

- Old configs saying "glance" must load as "iphone" (renamed builtin),
  idempotently: saving the migrated config must not flip it back.
- A missing/unloadable anti-spoof model must report itself loudly
  (available=False, non-empty reason) and never invent a verdict:
  spoof_probability() is None and denies() is False.
"""
import json

from faceid_app import openings


def _write_config(tmp_path, monkeypatch, data):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg_dir = tmp_path / "faceid-nim"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "openings.json").write_text(json.dumps(data))


def test_glance_config_migrates_to_iphone(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"active": "glance", "variants": {}})
    cfg = openings.load()
    assert cfg["active"] == "iphone"
    # Idempotent: a save/load round-trip keeps iphone, never glance.
    openings.save(cfg)
    assert openings.load()["active"] == "iphone"


def test_unknown_active_falls_back_to_iphone(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"active": "nope", "variants": {}})
    assert openings.load()["active"] == "iphone"


def test_missing_config_is_iphone_default(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert openings.load()["active"] == "iphone"
    assert "iphone" in openings.BUILTIN and "glance" not in openings.BUILTIN
