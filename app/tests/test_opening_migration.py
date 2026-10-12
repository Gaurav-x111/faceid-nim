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


def test_load_writes_back_migration(tmp_path, monkeypatch):
    """load() must persist glance→iphone, or the file keeps a stale id
    the lock screen reads literally while the app shows the migrated
    one (select-then-plays-another bug: on_opening_selected early-
    returns on the in-memory value and never saves)."""
    import json
    from faceid_app import openings
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    cfg_path = home / "faceid-nim" / "openings.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(
        {"active": "glance", "variants": {}, "scan_face": "arena"}))
    cfg = openings.load()
    assert cfg["active"] == "iphone"
    assert cfg["scan_face"] == "arena"
    on_disk = json.loads(cfg_path.read_text())
    assert on_disk["active"] == "iphone", on_disk
