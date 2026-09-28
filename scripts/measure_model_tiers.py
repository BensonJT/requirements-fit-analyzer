"""Measure the report.py model-tier thresholds (MODEL_LENS_MIN, MODEL_REQ_APPLY/REVIEW) against judged rows.

Out-of-sample by default: pass --labels-since (when the judged wave was imported) and --scores-before (when the
retrain that learned those labels started). Each labelled posting is then scored by the LATEST screen row
written before that retrain, i.e. by models that never saw its label. `screens` is append-only, so those rows
survive a rescreen. Without the two flags it measures in-sample on every judged row (quote those numbers only
as in-sample).

    .venv/bin/python scripts/measure_model_tiers.py --labels-since 2026-09-28 --scores-before '2026-09-28 14:00'
"""
import argparse
import duckdb

LENSES = [("process", "fit_process"), ("technical", "fit_technical"), ("ai", "fit_ai")]
GOOD = ("bullseye", "adjacent")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="db/jobsearch.duckdb")
    ap.add_argument("--labels-since")
    ap.add_argument("--scores-before")
    ap.add_argument("--lens-min", default="0.70,0.80,0.70", help="process,technical,ai")
    ap.add_argument("--req-apply", type=float, default=0.60)
    ap.add_argument("--req-review", type=float, default=0.40)
    a = ap.parse_args()
    con = duckdb.connect(a.db, read_only=True)

    # Placeholders appear in the SQL as: screen_where (in the CTE), then label_where.
    screen_where, label_where, params = "TRUE", "TRUE", []
    if a.scores_before:
        screen_where = "screened_at < ?"
        params.append(a.scores_before)
    if a.labels_since:
        label_where = "l.posting_id IN (SELECT posting_id FROM llm_labels WHERE judged_at >= ?)"
        params.append(a.labels_since)
    rows = con.execute(f"""
        WITH sc AS (SELECT * FROM screens WHERE {screen_where}
                    QUALIFY row_number() OVER (PARTITION BY posting_id ORDER BY screened_at DESC) = 1)
        SELECT v.tier, v.grade_process, v.grade_technical, v.grade_ai,
               sc.fit_process, sc.fit_technical, sc.fit_ai, sc.fit_required
        FROM vw_selection v JOIN vw_llm_labels_latest l USING (posting_id) JOIN sc USING (posting_id)
        WHERE {label_where} AND NOT v.adjudicated
    """, params).fetchall()
    print(f"rows measured: {len(rows)}  ({'out-of-sample' if a.scores_before else 'IN-SAMPLE'})")

    print("\nlens precision / recall vs judge bullseye|adjacent")
    for i, (name, _) in enumerate(LENSES):
        g = [r[1 + i] in GOOD for r in rows]
        p = [r[4 + i] for r in rows]
        line = [f"{name:9} pos={sum(g):4}"]
        for t in (0.5, 0.6, 0.7, 0.8, 0.9):
            hit = [x is not None and x >= t for x in p]
            tp = sum(h and y for h, y in zip(hit, g))
            prec = tp / sum(hit) if sum(hit) else float("nan")
            rec = tp / sum(g) if sum(g) else float("nan")
            line.append(f"@{t}: P {prec:.2f} R {rec:.2f} n={sum(hit)}")
        print("  " + " | ".join(line))

    mins = [float(x) for x in a.lens_min.split(",")]
    print(f"\nrequired model vs judge tier, among rows with a good lens (mins {mins})")
    goodlens = [r for r in rows if any((r[4 + i] or 0) >= mins[i] for i in range(3))]
    for lo in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
        band = [r for r in goodlens if (r[7] or 0) >= lo]
        c = {t: sum(1 for r in band if r[0] == t) for t in ("apply", "review", "hidden")}
        print(f"  required >= {lo}: n={len(band):4}  judge apply {c['apply']:4} review {c['review']:4} hidden {c['hidden']:4}")

    def tally(rs):
        return {t: sum(1 for r in rs if r[0] == t) for t in ("apply", "review", "hidden")}
    ap_rows = [r for r in goodlens if (r[7] or 0) >= a.req_apply]
    rv_rows = [r for r in goodlens if a.req_review <= (r[7] or 0) < a.req_apply]
    lo_rows = [r for r in goodlens if (r[7] or 0) < a.req_review]
    print(f"\nmodel Apply  (req >= {a.req_apply}): {len(ap_rows)} rows, judge {tally(ap_rows)}")
    print(f"model Review ({a.req_review} <= req < {a.req_apply}): {len(rv_rows)} rows, judge {tally(rv_rows)}")
    print(f"good lens, req < {a.req_review} (unclear): {len(lo_rows)} rows, judge {tally(lo_rows)}")


if __name__ == "__main__":
    main()
