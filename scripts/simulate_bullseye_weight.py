"""Read-only: how would the ONE rank move if the `bullseye` model weighed more inside rank_lens_best?

`vw_lens_fit.rank_lens_best` (backend/ats/store.py) maps fit_bullseye onto the 0.75-1.0 lens_value scale for a
row nobody has judged:  (lens_best + 0.25 + 0.75 * fit_bullseye) / 2.  Generalised to a weight w:

    (lens_best + (1 - w) + w * fit_bullseye) / 2        w = 0.75 reproduces today's view exactly (0.25 before 2026-10-19)

Everything else in rank_score is recomputed from the view's own columns and macros (effective_required_value,
level_value, the reject / lens_strong_p gate), so only the bullseye term changes. Nothing is written.

Two readings:
  * the movement: the top N under each weight, and the positions of any --watch posting ids;
  * the agreement with the user, once human grades exist: over pairs of graded postings whose human grade
    differs (bullseye > adjacent > stretch > wrong), the share each weight ranks in the human's order. Process
    grades come from human_lens_grades (finder.py feedback ingest, F4); a posting with no process grade falls
    back to the user's overall grade (finder.py mark --grade).

Context: 2026-10-19 deep review. The process lens is binary (bullseye + adjacent are its positives), so
coordinator roles score level with process-excellence roles, and the bullseye model is the term that can
separate them. Same evening, measured on the user's blind lens sheet (vault Tools/Blind_Reviews/Blind_Lens_Review_20261019.csv)
BEFORE the bullseye model trained on it: every higher w agreed with him more often, so the view moved to 0.75.
A run after a retrain is in-sample for every graded posting; only a newer blind sheet tests the next change.

    .venv/bin/python scripts/simulate_bullseye_weight.py
    .venv/bin/python scripts/simulate_bullseye_weight.py --weights 0.25,0.5,0.75 --top 25 --watch 35def563,686c8360
"""
import argparse
import itertools
import sys
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from backend.finder.feedback import USER_SCORER  # noqa: E402  (the scorer `mark --grade` writes)

CURRENT_W = 0.75   # the weight vw_lens_fit uses today (0.25 until 2026-10-19)
GRADE_ORDER = {"bullseye": 3, "adjacent": 2, "stretch": 1, "wrong": 0}

ROWS_SQL = """
    SELECT l.posting_id, l.employer, l.title, l.rank_score, l.lens_best, l.lens_breadth, l.fit_bullseye,
           l.grade_process IS NULL AND l.grade_technical IS NULL AND l.grade_ai IS NULL AS unjudged,
           l.effective_required_value * level_value(l.level_fit)
               * CASE WHEN l.verdict = 'reject' OR l.lens_best < lens_strong_p() THEN 0.0 ELSE 1.0 END AS mult,
           l.lens_best >= lens_strong_p() AS strong,
           coalesce(h.grade, u.grade) AS human_grade
    FROM vw_lens_fit l
    JOIN postings p USING (posting_id)
    LEFT JOIN vw_human_lens_grades_current h ON h.posting_id = l.posting_id AND h.lens = 'process'
    LEFT JOIN (SELECT posting_id, arg_max(grade, judged_at) AS grade FROM llm_labels
               WHERE scorer = ? AND grade IS NOT NULL GROUP BY posting_id) u ON u.posting_id = l.posting_id
    WHERE p.status = 'active' AND l.rank_score > 0
      AND (h.grade IS NOT NULL OR u.grade IS NOT NULL OR NOT (coalesce(l.decided, FALSE) OR coalesce(l.in_tracker, FALSE)))
"""


def rank_at(row, w):
    _, _, _, _, lens_best, breadth, fb, unjudged, mult, strong, _ = row
    if unjudged and fb is not None and strong:
        rlb = (lens_best + (1.0 - w) + w * fb) / 2.0
    else:
        rlb = lens_best
    return round(100.0 * (0.8 * rlb + 0.2 * max(0.0, (breadth or 0.0) - lens_best) / 2.0) * (mult or 0.0), 1)


def agreement(rows, w):
    graded = [(rank_at(r, w), GRADE_ORDER[r[10]]) for r in rows if r[10] in GRADE_ORDER]
    pairs = [(a, b) for a, b in itertools.combinations(graded, 2) if a[1] != b[1]]
    if not pairs:
        return None, 0
    right = sum(1 for a, b in pairs if (a[0] - b[0]) * (a[1] - b[1]) > 0)
    ties = sum(1 for a, b in pairs if a[0] == b[0])
    return (right + 0.5 * ties) / len(pairs), len(pairs)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="db/jobsearch.duckdb")
    ap.add_argument("--weights", default="0.25,0.5,0.75,1.0", help="comma list; CURRENT_W is today's view")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--watch", default="", help="comma list of posting_id prefixes to locate under each weight")
    a = ap.parse_args()
    weights = [float(x) for x in a.weights.split(",")]

    con = duckdb.connect(a.db, read_only=True)
    con.execute("SET enable_progress_bar = false")
    rows = con.execute(ROWS_SQL, [USER_SCORER]).fetchall()
    drift = sum(1 for r in rows if abs(rank_at(r, CURRENT_W) - (r[3] or 0)) > 0.11)
    print(f"{len(rows)} active ranked rows; formula check at w={CURRENT_W}: {drift} rows differ from the view's rank_score")

    for w in weights:
        score, n = agreement(rows, w)
        agree = f"agrees with the user on {score:.1%} of {n} graded pairs" if n else "no graded pairs yet"
        print(f"\n== w={w}: {agree}")
        for i, r in enumerate(sorted(rows, key=lambda r: -rank_at(r, w))[:a.top], 1):
            fb = "—" if r[6] is None else f"{r[6]:.2f}"
            mark = f"  [you: {r[10]}]" if r[10] else ""
            print(f"{i:3} {rank_at(r, w):5.1f}  bull {fb}  {r[0][:8]}  {r[1][:18]:18} {r[2][:48]}{mark}")

    watch = [x.strip() for x in a.watch.split(",") if x.strip()]
    if watch:
        print("\n== watched: position under " + " / ".join(f"w={w}" for w in weights))
        order = {w: [r[0] for r in sorted(rows, key=lambda r: -rank_at(r, w))] for w in weights}
        for pre in watch:
            hit = next((r for r in rows if r[0].startswith(pre)), None)
            if not hit:
                print(f"{pre}: not ranked (rank 0, decided or inactive)")
                continue
            pos = " / ".join(str(order[w].index(hit[0]) + 1) for w in weights)
            print(f"{pre}  {hit[1][:18]:18} {hit[2][:40]:40} {pos}")


if __name__ == "__main__":
    main()
