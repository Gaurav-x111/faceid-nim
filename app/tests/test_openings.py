import json
import zipfile

import pytest

from faceid_app import openings


def test_install_rejects_non_object_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    package = tmp_path / "bad.faceopen"
    with zipfile.ZipFile(package, "w") as z:
        z.writestr("opening.json", json.dumps(["not", "an", "object"]))

    with pytest.raises(ValueError):
        openings.install_package(str(package))


def _package(tmp_path, name="good.faceopen", **entries):
    package = tmp_path / name
    with zipfile.ZipFile(package, "w") as z:
        z.writestr("opening.json", json.dumps({"id": "t1", "name": "T1"}))
        for entry, data in entries.items():
            z.writestr(entry, data)
    return package


def test_install_rejects_absolute_and_parent_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    before = set(p.name for p in tmp_path.iterdir())
    for evil in ("/abs/path.svg", "../escape.svg", "a/../../escape.svg"):
        package = tmp_path / "evil.faceopen"
        with zipfile.ZipFile(package, "w") as z:
            z.writestr("opening.json", json.dumps({"id": "t1"}))
            z.writestr(evil, b"junk")
        with pytest.raises(ValueError):
            openings.install_package(str(package))
    # Nothing escaped the config dir and no variant was registered.
    assert set(p.name for p in tmp_path.iterdir()) == before | {"evil.faceopen"}
    assert not (tmp_path / "faceid-nim").exists()


def test_install_rejects_oversize_and_filecount(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    big = tmp_path / "big.faceopen"
    with zipfile.ZipFile(package := big, "w") as z:
        z.writestr("opening.json", json.dumps({"id": "t1"}))
        z.writestr("pad.bin", b"x" * (openings.MAX_UNPACK_BYTES + 1))
    with pytest.raises(ValueError):
        openings.install_package(str(big))
    many = tmp_path / "many.faceopen"
    with zipfile.ZipFile(many, "w") as z:
        z.writestr("opening.json", json.dumps({"id": "t1"}))
        for i in range(openings.MAX_UNPACK_FILES + 1):
            z.writestr(f"f{i}.bin", b"x")
    with pytest.raises(ValueError):
        openings.install_package(str(many))


def test_install_rejects_missing_opening_json(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    package = tmp_path / "noj.faceopen"
    with zipfile.ZipFile(package, "w") as z:
        z.writestr("logo.svg", b"<svg/>")
    with pytest.raises(ValueError):
        openings.install_package(str(package))


def test_install_rejects_encrypted_entry_cleanly(tmp_path, monkeypatch):
    # An encrypted opening.json raises RuntimeError on read, not
    # ValueError/KeyError (stdlib zipfile cannot even write encrypted
    # entries, so simulate the read side): the installer must convert
    # it to ValueError, not crash with a traceback.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    package = _package(tmp_path)
    real_zipfile = zipfile.ZipFile

    class BoomZip(real_zipfile):
        def read(self, name, pwd=None):
            raise RuntimeError("encrypted entry needs a password")

    monkeypatch.setattr(zipfile, "ZipFile", BoomZip)
    with pytest.raises(ValueError):
        openings.install_package(str(package))


def test_download_rejects_non_http_schemes(tmp_path, monkeypatch):
    for url in ("file:///etc/hostname",
                "ftp://example.com/x.faceopen",
                "javascript:alert(1)",
                "data:text/plain,hi"):
        with pytest.raises(ValueError):
            openings.download(url)
