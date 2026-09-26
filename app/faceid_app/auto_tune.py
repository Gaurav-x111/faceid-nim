#!/usr/bin/env python3
"""Universal hardware-aware auto-tune for faceid-nim.

Works for everyone: RGB-only laptops, IR laptops, hybrid rigs, or no
camera at all. After download it understands the hardware and suggests
a safe configuration — it never weakens security silently.

  python3 auto_tune.py [--npz eval/out/embeddings.npz --enroll-label me]

Output: one JSON object on stdout:
  {"camera_mode": "auto"|"rgb"|"ir"|"hybrid", "mode": ...,
   "strictness": ..., "tau": ..., "vote_k": ..., "vote_n": ...,
   "summary": "human line"}

Rules (universal, hardware-aware):
- No camera -> rgb defaults, tau 0.42 (conservative for RGB-only).
- RGB only  -> mode rgb, strictness light, tau 0.42.
- IR only   -> mode ir, strictness light, tau 0.40.
- RGB + IR  -> mode auto (dark->IR, lit->RGB per scan), tau 0.40.
- If --npz given, tau is refined from measured FAR (never below 0.35,
  never above 0.60); otherwise hardware defaults above are used.
"""
from __future__ import annotations

import argparse
import json
import sys

try:
    from . import camera_discovery as cd
except ImportError:  # direct script run
    import camera_discovery as cd


def hardware_suggestion() -> dict:
    try:
        cams = cd.discover()
    except Exception:
        cams = []
    picks = cd.pick(cams) if cams else {"rgb": None, "ir": None}
    has_rgb = picks.get("rgb") is not None
    has_ir = picks.get("ir") is not None
    # Room-light calibration: sample once so the summary tells the user
    # which spectrum auto will pick right now (dark->IR, lit->RGB).
    luma = None
    try:
        if has_rgb:
            luma = cd.sample_light(picks["rgb"].path)
    except Exception:
        luma = None
    light_note = ""
    if luma is not None:
        try:
            light_note = f"; {cd.light_label(luma)}"
        except Exception:
            light_note = f"; room luma {luma:.0f}"
    if has_rgb and has_ir:
        return {
            "camera_mode": "auto", "mode": "auto",
            "strictness": "light", "tau": 0.40,
            "vote_k": 3, "vote_n": 5,
            "auto_dark_luma": 28.0, "auto_light_luma": 42.0,
            "room_luma": luma,
            "summary": f"RGB+IR found ({picks['rgb'].path}+{picks['ir'].path}): auto (dark->IR, lit->RGB), tau 0.40{light_note}",
        }
    if has_ir:
        return {
            "camera_mode": "ir", "mode": "ir",
            "strictness": "light", "tau": 0.40,
            "vote_k": 3, "vote_n": 5,
            "summary": f"IR found ({picks['ir'].path}): ir mode, tau 0.40",
        }
    if has_rgb:
        return {
            "camera_mode": "rgb", "mode": "rgb",
            "strictness": "light", "tau": 0.42,
            "vote_k": 3, "vote_n": 5,
            "summary": f"RGB only ({picks['rgb'].path}): rgb mode, tau 0.42 (conservative)",
        }
    return {
        "camera_mode": "auto", "mode": "rgb",
        "strictness": "light", "tau": 0.42,
        "vote_k": 3, "vote_n": 5,
        "summary": "No camera found: safe RGB defaults, tau 0.42",
    }


def refine_tau(npz: str, enroll_label: str, base_tau: float) -> tuple[float, str]:
    try:
        import numpy as np
    except ImportError:
        return base_tau, "numpy missing, kept hardware default"
    try:
        d = np.load(npz, allow_pickle=True)
        X = d["X"].astype(np.float32)
        label = d["label"].astype(str)
    except Exception as e:
        return base_tau, f"could not read npz ({e}), kept hardware default"
    mine = label == enroll_label
    if not mine.any() or (~mine).sum() < 10:
        return base_tau, "too few samples, kept hardware default"
    templates = X[mine][:5]
    imp = (X[~mine] @ templates.T).max(axis=1)
    # tau at ~1e-3 FAR sample quantile, clamped to sane band.
    tau = float(np.quantile(imp, 0.999))
    tau = max(0.35, min(0.60, tau))
    return tau, f"measured tau {tau:.3f} from {imp.size} impostor scores"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", default=None)
    ap.add_argument("--enroll-label", default=None)
    args = ap.parse_args()
    sug = hardware_suggestion()
    if args.npz and args.enroll_label:
        tau, note = refine_tau(args.npz, args.enroll_label, sug["tau"])
        sug["tau"] = round(tau, 4)
        sug["summary"] += f"; {note}"
    print(json.dumps(sug))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
