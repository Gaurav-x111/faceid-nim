"""Opening-animation library for the faceid-nim settings app.

Declarative only.  A variant is JSON metadata plus an optional
SVG/PNG logo.  Nothing here ever runs code shipped by a stranger: the
shell extension (shell-ext/faceid@nim/opening.js) interprets a fixed
set of colours, easing curves and one logo image, so an installed
animation can repaint the unlock pill but can never execute in the
shell.

On-disk layout:

    ~/.config/faceid-nim/openings.json
        {"active": "logo", "variants": {"<id>": {<spec>}, ...}}
    ~/.config/faceid-nim/openings/<id>/logo.svg
        optional per-variant assets

Packages (".faceopen") are zips: opening.json + optional logo.svg +
optional preview.svg.  "Publishing to GitHub" is simply exporting a
package and stashing it anywhere; friends install it by raw URL (the
"Install from URL" path in the app) or from a file.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import urllib.request
import zipfile

# -- paths --------------------------------------------------------------

def _base() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(base, "faceid-nim")


def config_path() -> str:
    return os.path.join(_base(), "openings.json")


def openings_dir() -> str:
    return os.path.join(_base(), "openings")


def variant_dir(variant_id: str) -> str:
    return os.path.join(openings_dir(), _valid_id(variant_id))


# -- schema -------------------------------------------------------------

EASINGS = ["outCubic", "outBack", "outExpo"]

# Which scan-face the unlock pill draws. "arena" is the default premium
# scanner (halo + crest + dotted rings + beam + particles); "classic"
# is the original ring/sweep/face pill.
SCAN_FACES = ["arena", "classic"]
DEFAULT_SCAN_FACE = "arena"

BUILTIN = {
    "logo": {
        "kind": "builtin",
        "name": "App Logo",
        "description": "The Face Unlock logo fades in as the capsule "
                       "grows.",
    },
    "glow": {
        "kind": "builtin",
        "name": "Soft Glow",
        "description": "A calm accent halo, no logo lettering.",
    },
    "none": {
        "kind": "builtin",
        "name": "No splash",
        "description": "Go straight to the face scan, nothing in "
                       "between.",
    },
}

SPEC_KEYS = ("kind", "name", "description", "logo", "text",
             "bg", "accent", "ease", "fade_ms", "scale", "ring")

DEFAULT_CATALOG_URL = ("https://raw.githubusercontent.com/"
                       "Gaurav-x111/faceid-nim/main/openings/catalog.json")
PACKAGE_EXT = ".faceopen"
MAX_UNPACK_BYTES = 2 * 1024 * 1024
MAX_UNPACK_FILES = 20

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _valid_id(name: str) -> str:
    """Letters, digits, dash, underscore -> a safe directory name."""
    v = re.sub(r"[^A-Za-z0-9_-]", "-", str(name or "").strip()).strip("-")
    return (v or "opening")[:32].lower()


def _valid_color(value) -> str:
    value = str(value or "").strip()
    return value.upper() if _HEX.match(value) else ""


def _clean_spec(entry: dict) -> dict:
    spec = {k: entry[k] for k in SPEC_KEYS if k in entry}
    spec["kind"] = "custom"
    spec["name"] = str(spec.get("name", "Animation")).strip() or "Animation"
    spec["ease"] = spec.get("ease") if spec.get("ease") in EASINGS else "outCubic"
    try:
        spec["fade_ms"] = max(120, min(3000, int(spec.get("fade_ms", 420))))
    except (TypeError, ValueError):
        spec["fade_ms"] = 420
    for key in ("bg", "accent"):
        spec[key] = _valid_color(spec.get(key))
    spec["logo"] = str(spec.get("logo", "") or "")
    spec["text"] = str(spec.get("text", "") or "").strip() or None
    spec["scale"] = bool(spec.get("scale", True))
    spec["ring"] = bool(spec.get("ring", False))
    return spec


# -- config -------------------------------------------------------------

def default_config() -> dict:
    return {"active": "logo", "variants": {}, "scan_face": DEFAULT_SCAN_FACE}


def load() -> dict:
    cfg = default_config()
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return cfg
        variants = data.get("variants")
        if not isinstance(variants, dict):
            variants = {}
        cfg["variants"] = {
            k: _clean_spec(v) for k, v in variants.items()
            if isinstance(v, dict)
        }
        active = str(data.get("active", "logo"))
        cfg["active"] = active if active in all_ids(cfg) else "logo"
        face = str(data.get("scan_face", DEFAULT_SCAN_FACE))
        cfg["scan_face"] = face if face in SCAN_FACES else DEFAULT_SCAN_FACE
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg: dict) -> None:
    path = config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({k: cfg[k] for k in ("active", "variants", "scan_face")},
                      f, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass    # an animation choice is a convenience, never fatal


def all_ids(cfg: dict) -> list:
    return list(BUILTIN) + sorted(
        vid for vid in cfg.get("variants", {})
        if vid not in BUILTIN)


def spec(cfg: dict, variant_id: str) -> dict | None:
    if variant_id in BUILTIN:
        return dict(BUILTIN[variant_id], **{"id": variant_id})
    entry = cfg.get("variants", {}).get(variant_id)
    if not entry:
        return None
    return dict(_clean_spec(entry), **{"id": variant_id})


def set_active(cfg: dict, variant_id: str) -> bool:
    if variant_id not in all_ids(cfg):
        return False
    cfg["active"] = variant_id
    save(cfg)
    return True


def set_scan_face(cfg: dict, face: str = DEFAULT_SCAN_FACE) -> bool:
    """Pick the pill's scan-face style ('arena' | 'classic')."""
    if face not in SCAN_FACES:
        return False
    cfg["scan_face"] = face
    save(cfg)
    return True


def remove_variant(cfg: dict, variant_id: str) -> bool:
    if variant_id in BUILTIN or variant_id not in cfg.get("variants", {}):
        return False
    del cfg["variants"][variant_id]
    if cfg.get("active") == variant_id:
        cfg["active"] = "logo"
    save(cfg)
    shutil.rmtree(variant_dir(variant_id), ignore_errors=True)
    return True


def _unique_id(cfg: dict, name: str) -> str:
    base = _valid_id(name)
    vid, n = base, 1
    while vid in all_ids(cfg):
        n += 1
        vid = f"{base}-{n}"
    return vid


# -- create / export / install ------------------------------------------

def create_variant(cfg: dict, *, name: str, logo_path: str | None = None,
                   text: str = "", bg: str = "", accent: str = "",
                   ease: str = "outCubic", fade_ms: int = 420,
                   scale: bool = True, ring: bool = False) -> str:
    vid = _unique_id(cfg, name)
    d = variant_dir(vid)
    try:
        os.makedirs(d, exist_ok=True)
        if logo_path and os.path.isfile(logo_path) and not ring:
            logo_dest = os.path.join(d, os.path.basename(logo_path))
            shutil.copy2(logo_path, logo_dest)
            spec_logo = os.path.basename(logo_dest)
        else:
            spec_logo = ""
        entry = _clean_spec({
            "kind": "custom", "name": name, "logo": spec_logo,
            "text": text, "bg": bg, "accent": accent, "ease": ease,
            "fade_ms": fade_ms, "scale": scale and bool(spec_logo),
            "ring": ring,
        })
    except OSError:
        raise ValueError("Cannot write the animation files") from None
    cfg["variants"][vid] = entry
    save(cfg)
    return vid


_KEEP_LOGO = object()       # sentinel: leave the existing logo untouched


def update_variant(cfg: dict, variant_id: str, *, name: str | None = None,
                   text: str | None = None, bg: str | None = None,
                   accent: str | None = None, ease: str | None = None,
                   fade_ms: int | None = None, scale: bool | None = None,
                   ring: bool | None = None,
                   logo_path: str | None = _KEEP_LOGO) -> bool:
    entry = cfg.get("variants", {}).get(variant_id)
    if not entry:
        return False
    if name is not None:
        entry["name"] = str(name)
    if text is not None:
        entry["text"] = str(text).strip() or ""
    if bg is not None:
        entry["bg"] = _valid_color(bg)
    if accent is not None:
        entry["accent"] = _valid_color(accent)
    if ease is not None and ease in EASINGS:
        entry["ease"] = ease
    if fade_ms is not None:
        try:
            entry["fade_ms"] = max(120, min(3000, int(fade_ms)))
        except (TypeError, ValueError):
            pass
    if ring is not None:
        entry["ring"] = bool(ring)
    if scale is not None:
        entry["scale"] = bool(scale)
    if logo_path is not _KEEP_LOGO:
        # None / '' clears the logo; a file path swaps it in.
        d = variant_dir(variant_id)
        if logo_path and not entry.get("ring") and os.path.isfile(logo_path):
            try:
                os.makedirs(d, exist_ok=True)
                dest = os.path.join(d, os.path.basename(logo_path))
                shutil.copy2(logo_path, dest)
                entry["logo"] = os.path.basename(dest)
            except OSError:
                raise ValueError("Cannot write the new logo") from None
        else:
            entry["logo"] = ""
    cfg["variants"][variant_id] = _clean_spec(entry)
    save(cfg)
    return True


def export_package(cfg: dict, variant_id: str, dest_dir: str) -> str:
    """Write <id>.faceopen (a zip) into dest_dir and return its path."""
    meta = spec(cfg, variant_id)
    if not meta:
        raise ValueError("No such animation")
    package = {"kind": "custom",
               "id": variant_id,
               "name": meta["name"],
               "description": (meta.get("description") or
                               f"A {meta['name']} opening animation "
                               "for faceid-nim"),
               "text": meta.get("text") or "",
               "bg": meta.get("bg") or "",
               "accent": meta.get("accent") or "",
               "ease": meta.get("ease") or "outCubic",
               "fade_ms": meta.get("fade_ms") or 420,
               "scale": bool(meta.get("scale")),
               "ring": bool(meta.get("ring"))}
    try:
        os.makedirs(dest_dir, exist_ok=True)
        out = os.path.join(dest_dir, f"{variant_id}{PACKAGE_EXT}")
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("opening.json", json.dumps(package, indent=2))
            logo = meta.get("logo")
            if logo and not meta.get("ring"):
                src = os.path.join(variant_dir(variant_id), logo)
                if os.path.isfile(src):
                    z.write(src, "logo" + os.path.splitext(logo)[1])
    except (OSError, zipfile.BadZipFile):
        raise ValueError("Cannot export the package") from None
    return out


def install_package(package_path: str) -> str:
    """Validate a .faceopen zip, extract it and register the variant."""
    real = os.path.realpath(package_path)
    if not os.path.isfile(real):
        raise ValueError("The file does not exist")
    try:
        z = zipfile.ZipFile(real)
    except zipfile.BadZipFile:
        raise ValueError("That is not a .faceopen package") from None

    names = z.namelist()
    if len(names) > MAX_UNPACK_FILES or "opening.json" not in names:
        z.close()
        raise ValueError("Not a valid faceid-nim animation package")
    total = sum(i.file_size for i in z.infolist())
    if total > MAX_UNPACK_BYTES:
        z.close()
        raise ValueError("Package too large to install")
    if any(n.startswith("/") or ".." in n.split("/") for n in names):
        z.close()
        raise ValueError("Package contains unsafe file names")

    try:
        meta = json.loads(z.read("opening.json"))
    except (KeyError, ValueError):
        z.close()
        raise ValueError("Package has no readable opening.json") from None

    vid = _valid_id(meta.get("id") or meta.get("name"))
    if not vid or vid in BUILTIN:
        z.close()
        raise ValueError("Package has no usable id")

    logo_name = None
    for n in names:
        if n in ("logo.svg", "logo.png"):
            logo_name = n
            break

    d = variant_dir(vid)
    try:
        os.makedirs(openings_dir(), exist_ok=True)
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
        for n in names:
            if n == "opening.json":
                continue
            if z.getinfo(n).is_dir():
                continue
            target = os.path.join(d, os.path.basename(n))
            with z.open(n) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    except OSError:
        z.close()
        raise ValueError("Cannot write the package files") from None
    z.close()

    entry = _clean_spec({
        "kind": "custom", "name": meta.get("name") or vid,
        "description": meta.get("description") or "",
        "logo": logo_name or "",
        "text": meta.get("text") or "",
        "bg": meta.get("bg") or "",
        "accent": meta.get("accent") or "",
        "ease": meta.get("ease") or "outCubic",
        "fade_ms": meta.get("fade_ms") or 420,
        "scale": bool(meta.get("scale", True)),
        "ring": bool(meta.get("ring")),
    })
    cfg = load()
    cfg["variants"][vid] = entry
    save(cfg)
    return vid


def download(url: str) -> bytes:
    """Fetch a URL with a size cap (never block the caller's intent)."""
    req = urllib.request.Request(url, headers={"User-Agent": "faceid-nim/1"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = resp.read(MAX_UNPACK_BYTES + 1)
    if len(data) > MAX_UNPACK_BYTES:
        raise ValueError("Download too large to install")
    return data


def download_and_install(url: str) -> str:
    data = download(str(url).strip())
    if not data:
        raise ValueError("Empty download")
    with tempfile.NamedTemporaryFile(suffix=PACKAGE_EXT, delete=False) as f:
        f.write(data)
        tmp = f.name
    try:
        return install_package(tmp)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def fetch_catalog(url: str = DEFAULT_CATALOG_URL) -> list:
    """List of {"id", "name", "description", "url"} community packages."""
    data = download(url)
    try:
        entries = json.loads(data.decode("utf-8"))
    except ValueError:
        return []
    if not isinstance(entries, list):
        return []
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        pkg_url = str(e.get("url") or "").strip()
        name = str(e.get("name") or "").strip()
        if pkg_url and name:
            out.append({
                "id": _valid_id(e.get("id") or name),
                "name": name,
                "description": str(e.get("description") or "").strip(),
                "url": pkg_url,
            })
    return out