"""Model manifest loading with checksum verification.

Every model file is pinned by SHA-256 in models/manifest.toml. A model
that does not match its pin is never loaded: a swapped recognition
model is a silent authentication bypass.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib  # py311+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

DEFAULT_MANIFEST = Path(
    os.environ.get("FACEID_MANIFEST", "/usr/share/faceid-nim/models/manifest.toml")
)
DEFAULT_MODEL_DIR = Path(
    os.environ.get("FACEID_MODEL_DIR", "/var/lib/faceid-nim/models")
)


class ModelError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelSpec:
    name: str
    file: str
    sha256: str
    url: str
    license: str
    model_id: str          # goes into every template; changing it forces re-enroll
    input_size: tuple[int, int] = (112, 112)
    embedding_dim: int | None = None

    def path(self, model_dir: Path = DEFAULT_MODEL_DIR) -> Path:
        return model_dir / self.file


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def load_manifest(manifest: Path = DEFAULT_MANIFEST) -> dict[str, ModelSpec]:
    if not manifest.exists():
        raise ModelError(f"model manifest not found: {manifest}")
    with manifest.open("rb") as fh:
        data = tomllib.load(fh)
    out: dict[str, ModelSpec] = {}
    for name, entry in data.get("model", {}).items():
        out[name] = ModelSpec(
            name=name,
            file=entry["file"],
            sha256=entry["sha256"].lower(),
            url=entry.get("url", ""),
            license=entry.get("license", "unknown"),
            model_id=entry.get("model_id", name),
            input_size=tuple(entry.get("input_size", (112, 112))),  # type: ignore[arg-type]
            embedding_dim=entry.get("embedding_dim"),
        )
    return out


def resolve(name: str,
            manifest: Path = DEFAULT_MANIFEST,
            model_dir: Path = DEFAULT_MODEL_DIR,
            verify: bool = True) -> tuple[Path, ModelSpec]:
    """Return a verified on-disk path for the named model."""
    spec = load_manifest(manifest).get(name)
    if spec is None:
        raise ModelError(f"no model named {name!r} in {manifest}")
    path = spec.path(model_dir)
    if not path.exists():
        raise ModelError(f"model file missing: {path} (fetch it with faceid-nim fetch-models)")
    if verify:
        got = sha256_file(path)
        if got != spec.sha256:
            raise ModelError(
                f"checksum mismatch for {name}: manifest {spec.sha256}, file {got}"
            )
    return path, spec
