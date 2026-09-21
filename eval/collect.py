#!/usr/bin/env python3
"""Turn a directory of clips/images into an embedding dataset.

    eval/data/genuine/<label>/*.mp4|jpg     you, in varied conditions
    eval/data/impostor/<label>/*.mp4|jpg    other people
    eval/data/attack/<type>/*.mp4           print / screen / replay

    python eval/collect.py --root eval/data --out eval/out/embeddings.npz

Writes embeddings + labels + group. No images are kept.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vision"))

from faceid_vision.align import align                       # noqa: E402
from faceid_vision.quality import QualityConfig, assess     # noqa: E402
from faceid_vision.scan import build_engine                 # noqa: E402

MEDIA = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".jpg", ".jpeg", ".png"}


def frames_of(path: Path, stride: int):
    if path.suffix.lower() in {".jpg", ".jpeg", ".png"}:
        img = cv2.imread(str(path))
        if img is not None:
            yield img
        return
    cap = cv2.VideoCapture(str(path))
    i = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if i % stride == 0:
            yield frame
        i += 1
    cap.release()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path("eval/data"))
    ap.add_argument("--out", type=Path, default=Path("eval/out/embeddings.npz"))
    ap.add_argument("--stride", type=int, default=5)
    ap.add_argument("--max-per-clip", type=int, default=40)
    ap.add_argument("--model-dir", type=Path, default=None)
    ap.add_argument("--no-verify", action="store_true")
    args = ap.parse_args()

    engine = build_engine(model_dir=args.model_dir, verify=not args.no_verify)
    qcfg = QualityConfig()

    vecs, labels, groups, kinds = [], [], [], []
    for kind in ("genuine", "impostor", "attack"):
        base = args.root / kind
        if not base.is_dir():
            print(f"[skip] {base} missing")
            continue
        for label_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            for clip in sorted(p for p in label_dir.rglob("*") if p.suffix.lower() in MEDIA):
                n = 0
                for frame in frames_of(clip, args.stride):
                    if n >= args.max_per_clip:
                        break
                    face = engine.detector.largest(frame)
                    if face is None or not assess(frame, face, qcfg).ok:
                        continue
                    try:
                        v = engine.embedder(align(frame, face.landmarks,
                                                  engine.embedder.size))
                    except ValueError:
                        continue
                    vecs.append(v)
                    labels.append(label_dir.name)
                    groups.append(clip.stem)
                    kinds.append(kind)
                    n += 1
                print(f"{kind}/{label_dir.name}/{clip.name}: {n} embeddings")

    if not vecs:
        print("no embeddings collected", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(args.out,
                        X=np.stack(vecs).astype(np.float32),
                        label=np.array(labels), group=np.array(groups),
                        kind=np.array(kinds),
                        model_id=engine.embedder.model_id)
    print(f"\nwrote {len(vecs)} embeddings -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
