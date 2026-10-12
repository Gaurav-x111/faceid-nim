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
- IR only   -> mode ir, strictness light, tau 0.40, BUT only when the
  IR node actually delivers light (see below); a present-but-black
  node means the emitter is off, and suggesting ir would guarantee
  failure, so rgb is suggested instead with an honest summary.
- RGB + IR  -> mode auto (dark->IR, lit->RGB per scan), tau 0.40, BUT
  only when the IR node delivers light; otherwise rgb with a summary
  that names the dark IR node.
- If --npz given, tau is refined from measured FAR (never below 0.35,
  never above 0.60); otherwise hardware defaults above are used.

WHY THE IR NODE IS SAMPLED, NOT JUST DETECTED
Presence is not usability. An IR sensor with its illuminator off
returns near-black frames, and auto/ir would then fail every dark-room
scan -- worse, a dim-but-nonzero node (luma under the liveness
screen_dark floor) actively triggers spoof rejections instead of
merely matching poorly. So before suggesting a mode that uses IR, the
node is opened briefly and measured against IR_USABLE_LUMA (25, the
line below which liveness rejects the feed as screen-like). Under it,
IR modes are withheld. A lit-room reading cannot prove the emitter
works in the dark -- only an emitter toggle could, and there is no
generic Linux interface for that -- but a dim reading proves IR would
harm more than help right now, and that is enough to refuse it.
"""
from __future__ import annotations

import argparse
import json
import sys

try:
    from . import camera_discovery as cd
except ImportError:  # direct script run
    import camera_discovery as cd


# IR usability floor. Below this mean luma the sensor is not just dim,
# it is actively harmful: liveness.analyse_ir_face flags anything under
# (dark_lum - 10) = 25 as screen-like (deny cue), so an IR mode fed by
# such a node would reject real faces, not merely fail to match them.
# A lit emitter reads 80-200; ambient indoor IR without one reads 0-20.
# 25 cleanly separates "has real illumination" from "ambient noise".
IR_USABLE_LUMA = 25.0
# Below this the node is essentially unlit (emitter definitely off).
# Kept distinct from USABLE so the summary can say "black" vs "dim".
IR_BLACK_LUMA = 4.0


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
    # IR usability: a detected node is not a working sensor. Sample it;
    # under IR_USABLE_LUMA the feed would be rejected as screen-like by
    # liveness, so IR modes must be withheld, or scans fail (or worse,
    # false-reject). IR_BLACK_LUMA distinguishes "black" from "dim" so
    # the summary names the actual situation.
    ir_luma = None
    ir_live = False
    if has_ir:
        try:
            ir_luma = cd.sample_light(picks["ir"].path)
        except Exception:
            ir_luma = None
        ir_live = ir_luma is not None and ir_luma >= IR_USABLE_LUMA
    if has_rgb and has_ir and ir_live:
        return {
            "camera_mode": "auto", "mode": "auto",
            "strictness": "light", "tau": 0.40,
            "vote_k": 3, "vote_n": 5,
            "auto_dark_luma": 28.0, "auto_light_luma": 42.0,
            "room_luma": luma,
            "summary": f"RGB+IR found ({picks['rgb'].path}+{picks['ir'].path}, IR luma {ir_luma:.0f}): auto (dark->IR, lit->RGB), tau 0.40{light_note}",
        }
    if has_rgb and has_ir and not ir_live:
        # The honest case on emitter-less machines: the node is real,
        # but it cannot carry IR matching right now, so auto/ir would
        # fail or false-reject. Stay on RGB and say why.
        if ir_luma is None:
            why = (f"IR node {picks['ir'].path} present but could not be "
                   "sampled (busy?); IR modes withheld until it is verified "
                   "live -- see 'faceid-nim ir-emitter --probe'")
        elif ir_luma < IR_BLACK_LUMA:
            why = (f"IR node {picks['ir'].path} present but black "
                   f"(luma {ir_luma:.0f}): illuminator off, so auto/ir "
                   "would fail every dark-room scan; staying on RGB -- "
                   "see 'faceid-nim ir-emitter --probe'")
        else:
            why = (f"IR node {picks['ir'].path} present but dim "
                   f"(luma {ir_luma:.0f} < {IR_USABLE_LUMA:.0f}): only "
                   "ambient IR, no illuminator, and liveness would reject "
                   "this feed as screen-like; staying on RGB -- see "
                   "'faceid-nim ir-emitter --probe'")
        return {
            "camera_mode": "rgb", "mode": "rgb",
            "strictness": "light", "tau": 0.42,
            "vote_k": 3, "vote_n": 5,
            "room_luma": luma,
            "summary": f"RGB found ({picks['rgb'].path}); {why}{light_note}",
        }
    if has_ir and ir_live:
        return {
            "camera_mode": "ir", "mode": "ir",
            "strictness": "light", "tau": 0.40,
            "vote_k": 3, "vote_n": 5,
            "summary": f"IR found ({picks['ir'].path}, luma {ir_luma:.0f}): ir mode, tau 0.40",
        }
    if has_ir and not ir_live:
        # IR-only machine with an unusable sensor: ir mode cannot work,
        # and there is no RGB to fall back to. Report it plainly rather
        # than suggest a mode that fails or false-rejects.
        if ir_luma is None:
            why = (f"IR node {picks['ir'].path} present but could not be "
                   "sampled (busy?); cannot verify it delivers light")
        elif ir_luma < IR_BLACK_LUMA:
            why = (f"IR node {picks['ir'].path} present but black "
                   f"(luma {ir_luma:.0f}): illuminator off, ir mode "
                   "would fail")
        else:
            why = (f"IR node {picks['ir'].path} present but dim "
                   f"(luma {ir_luma:.0f} < {IR_USABLE_LUMA:.0f}): no "
                   "illuminator, and liveness would reject this feed as "
                   "screen-like")
        return {
            "camera_mode": "ir", "mode": "ir",
            "strictness": "light", "tau": 0.40,
            "vote_k": 3, "vote_n": 5,
            "summary": f"{why}; no RGB fallback on this machine -- see 'faceid-nim ir-emitter --probe'",
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
