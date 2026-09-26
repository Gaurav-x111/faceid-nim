"""Video/still resolver. Mirrors ScanMedia.videoResourceName in ScanAnimationView.swift.

Assets expected next to this package in ../assets/ OR in the macOS bundle
../glance/Resources/ (dev symlink). .deb install puts them in
/usr/share/glance-anim/assets/.
"""
from pathlib import Path

CANDIDATE_DIRS = [
    Path(__file__).resolve().parent / "assets",
    Path(__file__).resolve().parent.parent / "assets",
    Path("/usr/share/faceid-nim/linux-anim/glance_anim/assets"),
    Path("/usr/share/faceid-nim/linux-anim/assets"),
    Path("/usr/share/glance-anim/assets"),
]

# dev fallback: reuse macOS bundle resources without copying
DEV_MACOS_RES = Path(__file__).resolve().parent.parent.parent / "glance" / "Resources"


def asset_dir() -> Path | None:
    for d in CANDIDATE_DIRS:
        if d.is_dir() and any(d.iterdir()):
            return d
    if DEV_MACOS_RES.is_dir():
        return DEV_MACOS_RES
    for d in CANDIDATE_DIRS:
        if d.is_dir():
            return d
    return None


def asset_path(name: str, ext: str) -> Path | None:
    d = asset_dir()
    if d is None:
        return None
    p = d / f"{name}.{ext}"
    return p if p.exists() else None


MEDIA_FILES = {
    "idle": ("unlockstatic", "png"),
    "success": ("unlockanimation", "mp4"),
    "failure": ("unsuccessfulunlockanimation", "mp4"),
    "logo": ("logoanimation", "mp4"),
    "idle_loop": ("idleanimation", "mp4"),
}


def media_path(media: str) -> Path | None:
    key = MEDIA_FILES.get(media)
    if not key:
        return None
    return asset_path(*key)
