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
