#!/usr/bin/env python3
"""FAR / FRR / EER, and the threshold you should actually ship.

    python eval/far_frr.py --npz eval/out/embeddings.npz --enroll-label me \
        --target-far 1e-4

Method: hold out the enrolled templates (one clip), score every other
genuine frame against them (genuine distribution), score every
impostor frame against them (impostor distribution), then read TAU off
the impostor tail at your target FAR and report the FRR it costs.

Two things this script refuses to pretend:
  * A per-frame FAR is not a per-attempt FAR. k-of-n voting changes it,
    so both are printed.
  * "FAR against random strangers" hides look-alikes. Per-impostor
    worst case is printed separately, because that is the number that
    gets you.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np


def load(npz: Path):
    d = np.load(npz, allow_pickle=True)
    return (d["X"].astype(np.float32), d["label"].astype(str),
            d["group"].astype(str), d["kind"].astype(str))


def scores_against(templates: np.ndarray, probes: np.ndarray) -> np.ndarray:
    if probes.size == 0 or templates.size == 0:
        return np.empty(0, np.float32)
    return (probes @ templates.T).max(axis=1)


def far_at(impostor: np.ndarray, tau: float) -> float:
    return float((impostor >= tau).mean()) if impostor.size else float("nan")


def frr_at(genuine: np.ndarray, tau: float) -> float:
    return float((genuine < tau).mean()) if genuine.size else float("nan")


def eer(genuine: np.ndarray, impostor: np.ndarray) -> tuple[float, float]:
    taus = np.unique(np.concatenate([genuine, impostor]))
    best, best_tau = 1.0, 0.0
    for t in taus:
        f, r = far_at(impostor, t), frr_at(genuine, t)
        if abs(f - r) < best:
            best, best_tau = abs(f - r), float(t)
    return best_tau, (far_at(impostor, best_tau) + frr_at(genuine, best_tau)) / 2


def tau_for_far(impostor: np.ndarray, target: float) -> float:
    if impostor.size == 0 or not 0.0 < target <= 1.0:
        return float("nan")
    ordered = np.sort(impostor)
    index = max(0, int(np.ceil((1.0 - target) * ordered.size)) - 1)
    return float(np.nextafter(ordered[index], np.inf))


def voting_far(impostor_by_group: dict[str, np.ndarray], tau: float,
               k: int, n: int, trials: int = 20000, seed: int = 0) -> float:
    """Per-attempt FAR under k-of-n voting, by resampling n frames from
    a single impostor clip (frames within a clip are correlated, which
    is exactly the correlation an attacker gets for free)."""
    rng = np.random.default_rng(seed)
    groups = [v for v in impostor_by_group.values() if v.size >= n]
    if not groups:
        return float("nan")
    hits = 0
    for _ in range(trials):
        g = groups[rng.integers(len(groups))]
        idx = rng.integers(0, g.size, size=n)
        if int((g[idx] >= tau).sum()) >= k:
            hits += 1
    return hits / trials


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", type=Path, default=Path("eval/out/embeddings.npz"))
    ap.add_argument("--enroll-label", required=True)
    ap.add_argument("--enroll-group", default=None,
                    help="clip used as enrollment; default = first clip")
    ap.add_argument("--target-far", type=float, default=1e-4)
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--report", type=Path, default=Path("eval/out/report.md"))
    args = ap.parse_args()

    X, label, group, kind = load(args.npz)
    mine = label == args.enroll_label
    if not mine.any():
        print(f"no rows with label {args.enroll_label!r}")
        return 1

    groups = sorted(set(group[mine]))
    enroll_group = args.enroll_group or groups[0]
    enroll_mask = mine & (group == enroll_group)
    templates = X[enroll_mask]
    genuine = scores_against(templates, X[mine & ~enroll_mask])

    imp_mask = (~mine) & (kind != "attack")
    impostor = scores_against(templates, X[imp_mask])
    imp_by_group = {g: scores_against(templates, X[imp_mask & (group == g)])
                    for g in sorted(set(group[imp_mask]))}
    imp_by_person = {p: scores_against(templates, X[imp_mask & (label == p)])
                     for p in sorted(set(label[imp_mask]))}

    lines: list[str] = []
    def out(s=""):
        print(s)
        lines.append(s)

    out(f"# Recognition evaluation\n")
    out(f"- enrolled identity: `{args.enroll_label}` from clip `{enroll_group}`")
    out(f"- templates: {templates.shape[0]}  genuine probes: {genuine.size}  "
        f"impostor probes: {impostor.size}")
    out()

    if genuine.size:
        out(f"Genuine similarity   min {genuine.min():.4f}  "
            f"p05 {np.quantile(genuine, .05):.4f}  "
            f"median {np.median(genuine):.4f}  max {genuine.max():.4f}")
    if impostor.size:
        out(f"Impostor similarity  median {np.median(impostor):.4f}  "
            f"p99 {np.quantile(impostor, .99):.4f}  max {impostor.max():.4f}")
    out()

    if impostor.size and genuine.size:
        t_eer, e = eer(genuine, impostor)
        out(f"EER {e*100:.2f}% at tau {t_eer:.4f}")

        needed = int(round(1.0 / args.target_far))
        if impostor.size < needed:
            out(f"\n**Cannot measure FAR = {args.target_far:g} with "
                f"{impostor.size} impostor scores.** You need on the order of "
                f"{needed} to see that tail at all. The tau below is an "
                f"extrapolation, not a measurement.")
        tau = tau_for_far(impostor, args.target_far)
        out(f"\ntau for target FAR {args.target_far:g}: **{tau:.4f}**")
        out(f"  per-frame  FAR {far_at(impostor, tau)*100:.4f}%   "
            f"FRR {frr_at(genuine, tau)*100:.2f}%")
        vf = voting_far(imp_by_group, tau, args.k, args.n)
        out(f"  {args.k}-of-{args.n} attempt FAR {vf*100:.4f}% "
            f"(resampled within impostor clips)")

        out("\n## Per-impostor worst case")
        out("A single averaged FAR hides look-alikes. Siblings and twins "
            "live in this table.\n")
        out("| impostor | max similarity | frames over tau |")
        out("|---|---|---|")
        for p, sc in sorted(imp_by_person.items(),
                            key=lambda kv: -(kv[1].max() if kv[1].size else -1)):
            if not sc.size:
                continue
            out(f"| {p} | {sc.max():.4f} | {int((sc >= tau).sum())}/{sc.size} |")

        out("\n## Operating-point sweep\n")
        out("| tau | FAR (frame) | FRR (frame) |")
        out("|---|---|---|")
        for t in np.linspace(max(0.0, tau - 0.15), min(1.0, tau + 0.15), 13):
            out(f"| {t:.3f} | {far_at(impostor, t)*100:.4f}% | {frr_at(genuine, t)*100:.2f}% |")

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text("\n".join(lines) + "\n")
    print(f"\nwrote {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
