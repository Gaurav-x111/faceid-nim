"""Regression tests for eval/stats.py (Phase 2 Fix 4, finding 1E-2).

An evaluation script must refuse to dignify underpowered claims:
below the minimum sample count it prints INSUFFICIENT SAMPLES and no
percentage. These tests pin the interval math and the refusal path.
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stats import rate_report, rule_of_three, wilson_ci


def test_wilson_zero_of_ten():
    # k=0: lower bound is exactly 0, upper is the Wilson value
    # z^2/(n+z^2) ~= 0.2775 -- NOT the rule-of-three 0.3; both are
    # reported, so pin them separately.
    lo, hi = wilson_ci(0, 10)
    assert lo == 0.0
    assert abs(hi - 0.2775) < 1e-4


def test_wilson_ten_of_ten():
    lo, hi = wilson_ci(10, 10)
    assert hi == 1.0
    assert abs(lo - (1.0 - 0.2775)) < 1e-4


def test_wilson_brackets_point_estimate():
    for k, n in [(1, 30), (5, 50), (0, 5), (29, 30)]:
        lo, hi = wilson_ci(k, n)
        p = k / n
        assert lo <= p <= hi, f"CI must bracket {p}: [{lo}, {hi}]"
        assert 0.0 <= lo <= hi <= 1.0


def test_wilson_empty_sample_is_nan():
    lo, hi = wilson_ci(0, 0)
    assert math.isnan(lo) and math.isnan(hi)


def test_rule_of_three():
    assert rule_of_three(30) == 0.1
    assert abs(rule_of_three(100) - 0.03) < 1e-12


def test_refusal_below_minimum_prints_no_percentage():
    out = rate_report(0, 5, min_n=30)
    assert "INSUFFICIENT SAMPLES" in out
    assert "n=5" in out
    assert "%" not in out, "no percentage may be reported below minimum"


def test_report_always_shows_n_and_ci():
    out = rate_report(3, 40, min_n=30)
    assert "n=40" in out
    assert "95% CI" in out
    assert "%" in out


def test_report_zero_errors_states_rule_of_three():
    out = rate_report(0, 40, min_n=30)
    assert "rule of three" in out
    assert "n=40" in out
