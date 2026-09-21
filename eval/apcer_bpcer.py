#!/usr/bin/env python3
"""Presentation-attack metrics in ISO/IEC 30107-3 terms.

    APCER = attacks wrongly accepted, reported PER ATTACK TYPE
    BPCER = real users wrongly rejected
    ACER  = (max APCER + BPCER) / 2

    python eval/apcer_bpcer.py --root eval/data --strict heavy

Runs whole clips through the real worker, so it measures the shipped
fusion logic, not a reimplementation of it. Report APCER per type: an
average across attack types lets a system that is hopeless against
screen replay hide behind being good at printed photos.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vision"))

from faceid_vision.camera import open_source              # noqa: E402
from faceid_vision.fuse import Strictness, permits        # noqa: E402
from faceid_vision.scan import ScanConfig, build_engine   # noqa: E402

CLIPS = {".mp4", ".mkv", ".avi", ".mov", ".webm"}


def run_clip(engine, path: Path, cfg: ScanConfig) -> tuple[bool, str, object]:
    with open_source(str(path)) as cam:
        res = engine.scan(cam, cfg)
    ok, why = permits(res.liveness, cfg.strictness)
    return ok, why, res.liveness


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path("eval/data"))
    ap.add_argument("--strict", default="heavy",
                    choices=[s.value for s in Strictness])
    ap.add_argument("--timeout-ms", type=int, default=6000)
    ap.add_argument("--model-dir", type=Path, default=None)
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--report", type=Path, default=Path("eval/out/antispoof.md"))
    args = ap.parse_args()

    engine = build_engine(model_dir=args.model_dir, verify=not args.no_verify)
    cfg = ScanConfig(timeout_ms=args.timeout_ms, strictness=Strictness(args.strict))

    lines = [f"# Anti-spoofing evaluation (strictness = {args.strict})\n"]
    per_type: dict[str, list[bool]] = {}

    attack_root = args.root / "attack"
    if attack_root.is_dir():
        for tdir in sorted(p for p in attack_root.iterdir() if p.is_dir()):
            accepted = []
            for clip in sorted(p for p in tdir.rglob("*") if p.suffix.lower() in CLIPS):
                ok, why, live = run_clip(engine, clip, cfg)
                accepted.append(ok)
                mark = "ACCEPTED (bad)" if ok else "blocked"
                print(f"attack/{tdir.name}/{clip.name}: {mark} "
                      f"deny={live.deny} confirm={live.confirm} {why}")
            if accepted:
                per_type[tdir.name] = accepted

    bona_fide = []
    gen_root = args.root / "genuine"
    if gen_root.is_dir():
        for clip in sorted(p for p in gen_root.rglob("*") if p.suffix.lower() in CLIPS):
            ok, why, live = run_clip(engine, clip, cfg)
            bona_fide.append(ok)
            print(f"genuine/{clip.name}: {'passed' if ok else 'REJECTED (bad)'} "
                  f"deny={live.deny} confirm={live.confirm} {why}")

    lines.append("| attack type | clips | APCER |")
    lines.append("|---|---|---|")
    apcers = []
    for t, acc in per_type.items():
        a = sum(acc) / len(acc)
        apcers.append(a)
        lines.append(f"| {t} | {len(acc)} | {a*100:.1f}% |")

    bpcer = 1.0 - (sum(bona_fide) / len(bona_fide)) if bona_fide else float("nan")
    lines.append(f"\n- BPCER (bona-fide rejected): **{bpcer*100:.1f}%** "
                 f"over {len(bona_fide)} clips")
    if apcers:
        acer = (max(apcers) + bpcer) / 2
        lines.append(f"- worst-case APCER: **{max(apcers)*100:.1f}%**")
        lines.append(f"- ACER (worst APCER + BPCER)/2: **{acer*100:.1f}%**")
    lines.append("\nAPCER is reported per attack type on purpose. Averaging "
                 "across types lets a system that fails against screen replay "
                 "hide behind good results on printed photos.")

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
