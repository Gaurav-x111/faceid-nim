"""Per-user app preferences for faceid-nim's settings window.

The daemon config is root-owned and machine-wide; things the *user*
wants (which camera mode to prefer, whether to pin a specific device
by identity) live in ~/.config/faceid-nim/app.json, written atomically.

Keys, with defaults:

  camera_mode : "auto" | "ir" | "rgb"  -- what the user asked for.
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


DEFAULTS = {"camera_mode": "auto", "rgb_dev": "", "ir_dev": ""}


def load() -> dict:
    path = pref_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return dict(DEFAULTS)
    merged = dict(DEFAULTS)
    for k, v in data.items():
        if k in merged and isinstance(v, type(merged[k])):
            merged[k] = v
    if merged["camera_mode"] not in ("auto", "ir", "rgb"):
        merged["camera_mode"] = "auto"
    return merged


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