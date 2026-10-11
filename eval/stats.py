#!/usr/bin/env python3
"""Honest small-sample reporting for the faceid-nim evaluations.

A bare "APCER 0.0%" over 3 clips reads as a security claim and is not
one. Every rate printed by eval/apcer_bpcer.py and eval/far_frr.py goes
through here:

* Wilson score 95% confidence interval around k/n.
* Zero observed errors: the interval still applies, and the "rule of
  three" upper bound (3/n at 95%) is stated alongside -- it is the
  clearest way to say what zero-of-n actually rules out.
* Below ``min_n`` samples: no percentage at all, just
  "INSUFFICIENT SAMPLES (n=...)".

Shared by both scripts so the two evaluations cannot drift into
different standards of honesty (finding 1E-2).
"""
from __future__ import annotations

import math

Z95 = 1.96  # two-sided 95% normal quantile


def wilson_ci(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for k successes in n trials.

    Returns (nan, nan) for n <= 0: there is no interval without data,
    and callers must hit the INSUFFICIENT SAMPLES path instead.
    """
    if n <= 0 or k < 0 or k > n:
        return (float("nan"), float("nan"))
    p = k / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def rule_of_three(n: int) -> float:
    """95% upper bound on the true rate after zero errors in n trials."""
    if n <= 0:
        return float("nan")
    return 3.0 / n


def rate_report(k: int, n: int, min_n: int = 30) -> str:
    """One-line honest rendering of k/n.

    Below min_n: refusal, with n stated and NO percentage. At or above:
    percentage plus 95% Wilson CI plus n; zero errors additionally
    state the rule-of-three bound.
    """
    if n < min_n:
        return f"INSUFFICIENT SAMPLES (n={n}): no percentage reported"
    lo, hi = wilson_ci(k, n)
    pct = 100.0 * k / n
    out = f"{pct:.1f}% (95% CI {100.0*lo:.1f}--{100.0*hi:.1f}%, n={n})"
    if k == 0:
        out += f"; rule of three upper bound {100.0*rule_of_three(n):.1f}%"
    return out
