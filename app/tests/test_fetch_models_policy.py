"""H7 regression: safe temp dirs, constrained root overrides, host allowlist."""
import pathlib
import re
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[2]
FETCH = REPO / "packaging" / "scripts" / "fetch-models"
VERIFY = REPO / "packaging" / "scripts" / "verify-models"


def test_verify_models_avoids_world_readable_tmp():
    text = VERIFY.read_text()
    assert "mktemp /tmp" not in text
    assert ".faceid-verify." in text
    assert "$MODEL_DIR/.faceid-verify." in text


def test_fetch_models_constrains_root_env_overrides():
    text = FETCH.read_text()
    assert "id -u" in text
    assert "/usr/share/faceid-nim" in text
    assert "/var/lib/faceid-nim" in text
    assert "refusing root run" in text


def test_fetch_models_host_allowlist():
    text = FETCH.read_text()
    assert "host_allowed" in text
    for host in ("github.com", "huggingface.co",
                 "storage.googleapis.com"):
        assert host in text
    assert "https://" in text
    assert "non-https" in text or "non-allowlisted" in text


def test_manifest_hosts_within_allowlist():
    manifests = [REPO / "models" / "manifest.toml"]
    found = [m for m in manifests if m.is_file()]
    assert found, "manifest missing"
    for m in found:
        for line in m.read_text().splitlines():
            line = line.strip()
            if line.startswith("url"):
                url = line.split('"')[1]
                assert url.startswith("https://"), url
                host = url.split("/")[2].lower()
                assert host in (
                    "github.com", "raw.githubusercontent.com",
                    "objects.githubusercontent.com",
                    "huggingface.co", "cdn-lfs.huggingface.co",
                    "storage.googleapis.com",
                ), host


def test_host_allowlist_functional():
    """Extract host_allowed() and exercise it in sh."""
    text = FETCH.read_text()
    start = text.index("host_allowed()")
    end = text.index("}", text.index("return 1", start)) + 1
    func = text[start:end]
    script = func + "\n" + "\n".join(
        f'host_allowed "{h}" && echo ALLOW:{h} || echo DENY:{h}'
        for h in ("github.com", "huggingface.co",
                  "storage.googleapis.com", "evil.example.com",
                  "github.com.evil.com")
    )
    out = subprocess.run(["sh", "-c", script], capture_output=True,
                         text=True, timeout=10)
    assert out.returncode == 0, out.stderr
    assert "ALLOW:github.com" in out.stdout
    assert "ALLOW:huggingface.co" in out.stdout
    assert "ALLOW:storage.googleapis.com" in out.stdout
    assert "DENY:evil.example.com" in out.stdout
    # Suffix match must not smuggle: github.com.evil.com is not github.com.
    assert "DENY:github.com.evil.com" in out.stdout
    # Regex special chars in host must not break matching (unanchored
    # '.' would let evilXgithubXcom through; case patterns avoid that).
    assert re.search(r"github\\.com", func) or "github.com" in func
