"""Per-user app preferences for faceid-nim's settings window.

The daemon config is root-owned and machine-wide; things the *user*
wants (which camera mode to prefer, whether to pin a specific device
by identity) live in ~/.config/faceid-nim/app.json, written atomically.

Keys, with defaults:

  camera_mode : "auto" | "rgb" | "ir" | "hybrid"  -- what the user asked for.
  rgb_dev     : str | ""   -- the specific /dev/videoN pinned for RGB.
  ir_dev      : str | ""   -- the specific /dev/videoN pinned for IR.

An empty *_dev means "re-detect on the next refresh".  The resolved
values (daemon mode + camera/ir_camera paths) are pushed to the daemon
separately, so this file is only a memory of intent, never an ACL.
"""

from __future__ import annotations

import json
import os


def pref_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(base, "faceid-nim", "app.json")


DEFAULTS = {"camera_mode": "auto", "rgb_dev": "", "ir_dev": "",
            "speak_greeting": False,
            "greeting_text": "Welcome {name}",
            "greeting_voice": "",
            "greeting_rate": 0,
            "animations_enabled": True}


def load() -> dict:
    path = pref_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return dict(DEFAULTS)
    if not isinstance(data, dict):
        return dict(DEFAULTS)
    merged = dict(DEFAULTS)
    for k, v in data.items():
        if k in merged and isinstance(v, type(merged[k])):
            merged[k] = v
    if merged["camera_mode"] not in ("auto", "ir", "rgb", "hybrid"):
        merged["camera_mode"] = "auto"
    # Custom spoken greeting: free text with optional {name}, capped
    # so a pasted paragraph can never become a speech flood.
    try:
        gt = str(merged.get("greeting_text", "Welcome {name}"))[:120]
    except Exception:
        gt = "Welcome {name}"
    merged["greeting_text"] = gt or "Welcome {name}"
    try:
        gv = str(merged.get("greeting_voice", "") or "")[:48]
    except Exception:
        gv = ""
    merged["greeting_voice"] = gv
    try:
        gr = int(merged.get("greeting_rate", 0))
    except (TypeError, ValueError):
        gr = 0
    merged["greeting_rate"] = max(-100, min(100, gr))
    return merged


# Human-readable titles for daemon diagnostics keys, so the app and the
# first-run wizard show "Worker reachable" instead of "worker_reachable".
DIAG_TITLES = {
    "worker_reachable": "Worker reachable",
    "models_ready": "Models ready",
    "camera": "Color camera",
    "ir_camera": "IR camera",
    "vote": "Match voting",
    "strictness": "Strictness",
    "mode": "Camera mode",
    "tau": "Match threshold",
    "timeline_enabled": "Login timeline",
    "liveness": "Liveness checks running",
    "accel": "Inference device",
    "worker": "Worker",
}


def diag_title(key: str) -> str:
    """Friendly list title for a diagnostics key (never raises)."""
    try:
        if key in DIAG_TITLES:
            return DIAG_TITLES[key]
        pretty = str(key).replace("_", " ").capitalize()
        # An empty Adw row title renders as a visibly broken row, and the
        # daemon is what feeds these keys. Never return nothing.
        return pretty or "Unknown setting"
    except Exception:
        return "Unknown setting"


def save(prefs: dict) -> None:
    path = pref_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(prefs, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass    # prefs are a convenience; never crash the app over them