"""Agreement statistics for the re-grade study (scripts/regrade_study.py). Pure functions, stdlib only.

The study compares two calls on the same postings: the user's original blind Required-fit call ("then") and his
blind re-grade weeks later ("now"). Each statistic answers one plain question:

- percent agreement: how often did the two calls match?
- Cohen's kappa: how much better than guessing did they match, given how often each call was used?
- Gwet's AC1: the same question, built to stay sensible when one call dominates (most postings fail), where kappa
  can read low even though the calls nearly always match (the prevalence trap).
- weighted agreement: the sample is drawn half meets, half fails on purpose; reweighting each stratum by its
  share of the whole gold set estimates the agreement the gold set as a whole would show.
"""
import math
import random

CATS = ("meets", "fails")


def confusion(pairs):
    """{(then, now): count} over the four cells."""
    out = {(a, b): 0 for a in CATS for b in CATS}
    for a, b in pairs:
        out[(a, b)] += 1
    return out


def percent_agreement(pairs):
    pairs = list(pairs)
    return sum(a == b for a, b in pairs) / len(pairs) if pairs else float("nan")


def _marginals(pairs):
    n = len(pairs)
    p1 = {k: sum(a == k for a, _ in pairs) / n for k in CATS}
    p2 = {k: sum(b == k for _, b in pairs) / n for k in CATS}
    return p1, p2


def cohen_kappa(pairs):
    pairs = list(pairs)
    if not pairs:
        return float("nan")
    po = percent_agreement(pairs)
    p1, p2 = _marginals(pairs)
    pe = sum(p1[k] * p2[k] for k in CATS)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def gwet_ac1(pairs):
    pairs = list(pairs)
    if not pairs:
        return float("nan")
    po = percent_agreement(pairs)
    p1, p2 = _marginals(pairs)
    q = len(CATS)
    pe = sum(((p1[k] + p2[k]) / 2) * (1 - (p1[k] + p2[k]) / 2) for k in CATS) / (q - 1)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def wilson(k, n, z=1.96):
    """95% Wilson interval for a proportion k/n."""
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def bootstrap_ci(pairs, stat, reps=2000, seed=11):
    """95% percentile bootstrap interval for stat(pairs)."""
    pairs = list(pairs)
    rng = random.Random(seed)
    vals = []
    for _ in range(reps):
        v = stat([pairs[rng.randrange(len(pairs))] for _ in pairs])
        if not math.isnan(v):
            vals.append(v)
    vals.sort()
    if not vals:
        return (float("nan"), float("nan"))
    return (vals[int(0.025 * (len(vals) - 1))], vals[int(0.975 * (len(vals) - 1))])


def weighted_agreement(pairs, population_share):
    """Agreement within each original-call stratum, reweighted by that call's share of the whole gold set."""
    pairs = list(pairs)
    total = 0.0
    for k, w in population_share.items():
        s = [(a, b) for a, b in pairs if a == k]
        if s:
            total += w * percent_agreement(s)
    return total


def summary(pairs, population_share=None):
    pairs = list(pairs)
    n = len(pairs)
    agree = sum(a == b for a, b in pairs)
    out = {
        "n": n,
        "agree": agree,
        "percent_agreement": agree / n if n else float("nan"),
        "agreement_ci": wilson(agree, n),
        "kappa": cohen_kappa(pairs),
        "kappa_ci": bootstrap_ci(pairs, cohen_kappa),
        "ac1": gwet_ac1(pairs),
        "ac1_ci": bootstrap_ci(pairs, gwet_ac1),
        "confusion": confusion(pairs),
        "by_original": {k: (sum(a == b for a, b in pairs if a == k), sum(a == k for a, _ in pairs)) for k in CATS},
    }
    if population_share:
        out["weighted_agreement"] = weighted_agreement(pairs, population_share)
    return out


def select_sample(rows, n_meets, n_fails, seed):
    """Stratified draw: rows are dicts with posting_id and original_fit. Returns the chosen rows, shuffled
    together (one presentation order for both strata), each with item_no 1..n."""
    rng = random.Random(seed)
    meets = sorted((r for r in rows if r["original_fit"] == "meets"), key=lambda r: r["posting_id"])
    fails = sorted((r for r in rows if r["original_fit"] == "fails"), key=lambda r: r["posting_id"])
    if len(meets) < n_meets or len(fails) < n_fails:
        raise ValueError(f"not enough rows: {len(meets)} meets (need {n_meets}), {len(fails)} fails (need {n_fails})")
    chosen = rng.sample(meets, n_meets) + rng.sample(fails, n_fails)
    rng.shuffle(chosen)
    return [dict(r, item_no=i) for i, r in enumerate(chosen, 1)]
