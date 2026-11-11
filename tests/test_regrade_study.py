"""Re-grade study statistics and sample draw (backend/finder/regrade_stats.py). No database, no network."""
import math

import pytest

from backend.finder import regrade_stats as rs


def P(n_mm=0, n_mf=0, n_fm=0, n_ff=0):
    return ([("meets", "meets")] * n_mm + [("meets", "fails")] * n_mf
            + [("fails", "meets")] * n_fm + [("fails", "fails")] * n_ff)


def test_perfect_agreement():
    pairs = P(n_mm=10, n_ff=10)
    assert rs.percent_agreement(pairs) == 1.0
    assert rs.cohen_kappa(pairs) == pytest.approx(1.0)
    assert rs.gwet_ac1(pairs) == pytest.approx(1.0)


def test_kappa_hand_computed():
    # 20 meets/meets, 5 meets/fails, 10 fails/meets, 15 fails/fails (n=50)
    pairs = P(20, 5, 10, 15)
    po = 35 / 50
    p1m, p2m = 25 / 50, 30 / 50
    pe = p1m * p2m + (1 - p1m) * (1 - p2m)
    assert rs.percent_agreement(pairs) == pytest.approx(po)
    assert rs.cohen_kappa(pairs) == pytest.approx((po - pe) / (1 - pe))


def test_ac1_hand_computed():
    pairs = P(20, 5, 10, 15)
    po = 35 / 50
    pim = (25 / 50 + 30 / 50) / 2
    pe = (pim * (1 - pim) + (1 - pim) * pim) / 1
    assert rs.gwet_ac1(pairs) == pytest.approx((po - pe) / (1 - pe))


def test_prevalence_trap_kappa_low_ac1_high():
    # 90% agreement, but nearly everything is "fails": kappa reads low, AC1 does not.
    pairs = P(n_mm=1, n_mf=4, n_fm=6, n_ff=89)
    assert rs.percent_agreement(pairs) == pytest.approx(0.90)
    assert rs.cohen_kappa(pairs) < 0.2
    assert rs.gwet_ac1(pairs) > 0.85


def test_wilson_bounds():
    lo, hi = rs.wilson(45, 60)
    assert 0.6 < lo < 0.75 < hi < 0.86
    assert rs.wilson(0, 0)[0] != rs.wilson(0, 0)[0]   # nan


def test_weighted_agreement_reweights_strata():
    # meets stratum: 15 of 30 kept (50%); fails stratum: 27 of 30 kept (90%)
    pairs = P(n_mm=15, n_mf=15, n_fm=3, n_ff=27)
    assert rs.percent_agreement(pairs) == pytest.approx(0.70)
    assert rs.weighted_agreement(pairs, {"meets": 0.2, "fails": 0.8}) == pytest.approx(0.2 * 0.5 + 0.8 * 0.9)


def test_summary_and_bootstrap_are_deterministic():
    pairs = P(20, 5, 10, 15)
    a, b = rs.summary(pairs), rs.summary(pairs)
    assert a["kappa_ci"] == b["kappa_ci"]
    assert a["kappa_ci"][0] <= a["kappa"] <= a["kappa_ci"][1]
    assert a["by_original"] == {"meets": (20, 25), "fails": (15, 25)}


def test_select_sample_stratified_shuffled_numbered():
    rows = [{"posting_id": f"m{i:02d}", "original_fit": "meets"} for i in range(29)] + \
           [{"posting_id": f"f{i:02d}", "original_fit": "fails"} for i in range(89)]
    s = rs.select_sample(rows, 29, 31, seed=1)
    assert len(s) == 60
    assert sum(r["original_fit"] == "meets" for r in s) == 29
    assert [r["item_no"] for r in s] == list(range(1, 61))
    assert len({r["posting_id"] for r in s}) == 60
    assert s == rs.select_sample(rows, 29, 31, seed=1)                  # same seed, same study
    assert [r["original_fit"] for r in s[:10]] != ["meets"] * 10         # strata are interleaved


def test_select_sample_refuses_short_strata():
    rows = [{"posting_id": "m1", "original_fit": "meets"}]
    with pytest.raises(ValueError):
        rs.select_sample(rows, 2, 0, seed=1)
