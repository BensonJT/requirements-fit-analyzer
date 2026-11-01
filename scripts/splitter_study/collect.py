"""Splitter study (read-only): what headings JDs use, what follows them, and where the current splitter misfiles.

Usage: ~/jobsearch/.venv/bin/python splitter_study.py OUT.jsonl [--per-employer 40] [--workers 8] [--population]
Writes one JSON line per posting; analysis is a separate step.
"""
import argparse
import json
import os
import re
import sys
from multiprocessing import Pool

sys.path.insert(0, os.environ.get("JOBSEARCH_ROOT") or os.path.expanduser("~/jobsearch"))
import duckdb  # noqa: E402

from backend.finder import judge2  # noqa: E402
from backend.finder import requirements as R  # noqa: E402
from backend.finder.labels import strip_boilerplate  # noqa: E402

DUTY_VERBS = (r"lead|leads|manage|manages|develop|develops|partner|partners|drive|drives|own|owns|build|builds|design"
              r"|designs|collaborate|collaborates|support|supports|coordinate|coordinates|oversee|oversees|create"
              r"|creates|ensure|ensures|define|defines|establish|establishes|serve|serves|act|acts|analyze|analyzes"
              r"|identify|identifies|implement|implements|deliver|delivers|monitor|monitors|prepare|prepares|provide"
              r"|provides|maintain|maintains|conduct|conducts|facilitate|facilitates|track|tracks|present|presents"
              r"|communicate|communicates|work|works|participate|participates|perform|performs|review|reviews"
              r"|translate|translates|evaluate|evaluates|assist|assists|plan|plans|report|reports|champion|champions"
              r"|run|runs|operate|operates|direct|directs|guide|guides|influence|influences|advise|advises|use|uses"
              r"|leverage|leverages|engage|engages|respond|responds|recommend|recommends|serve as|you will|you.ll")
DUTY = re.compile(rf"^\W*(?:{DUTY_VERBS})\b", re.I)
QUAL = re.compile(r"\b(?:years?|degree|bachelor|master|ph\.?d|certif\w*|licen[cs]\w*|clearance|experience (?:in|with|"
                  r"leading|managing|working)|proficien\w*|knowledge of|ability to|demonstrated|proven|familiarity"
                  r"|expertise|skills?\b|understanding of|track record|background in)", re.I)
PREF_CUE = re.compile(r"\b(?:preferred|a plus|nice to have|bonus|desired|desirable|ideally|is an asset|advantageous"
                      r"|highly valued|not required|good to have)\b", re.I)
NOISE = re.compile(judge2.JUDGE_NOISE.pattern, judge2.JUDGE_NOISE.flags)


def norm_heading(s):
    s = R._BULLET.sub("", s).strip().rstrip(":").strip()
    s = re.sub(r"[’']", "'", s)
    s = re.sub(r"\s+", " ", s).lower()
    return re.sub(r"[^\w' &/+-]", "", s).strip()[:80]


def profile(lines):
    n = len(lines)
    if not n:
        return {"n": 0}
    return {"n": n,
            "qual": sum(bool(QUAL.search(l)) for l in lines) / n,
            "duty": sum(bool(DUTY.search(R._BULLET.sub("", l))) for l in lines) / n,
            "pref": sum(bool(PREF_CUE.search(l)) for l in lines) / n,
            "noise": sum(bool(NOISE.search(l)) for l in lines) / n,
            "cert": sum(bool(re.search(r"\bcertifi|\bcertified\b|®", l, re.I)) and len(l) < 90 for l in lines) / n}


def study(row):
    pid, employer, platform, text = row
    try:
        lines = R.rejoin_lines(strip_boilerplate(R._unwrap_paragraphs(text or "")))
        kinds = [R._heading_kind(l) if l else None for l in lines]
        heads = []   # (index, text, kind or 'cand')
        for i, (l, k) in enumerate(zip(lines, kinds)):
            if not l:
                continue
            if k is not None:
                heads.append((i, l, k))
            elif not R._BULLET.match(l) and (R._subheading(l) or (l.rstrip().endswith(":") and len(l) <= 90)):
                heads.append((i, l, "cand"))
        out_heads = []
        for j, (i, l, k) in enumerate(heads):
            end = heads[j + 1][0] if j + 1 < len(heads) else len(lines)
            body = [x for x in lines[i + 1:end] if x][:12]
            out_heads.append({"h": norm_heading(l), "raw": l.strip()[:100], "kind": k, **profile(body),
                              "sample": [b[:140] for b in body[:3]]})
        req = judge2.section_lines(text, "required")
        resp = judge2.section_lines(text, "responsibility")
        pref = judge2.section_lines(text, "preferred")
        units = R.split_requirements(text)
        intro_n = sum(1 for u in units if u.section in ("intro", "body"))
        return {"pid": pid, "employer": employer, "platform": platform, "n_req": len(req), "n_resp": len(resp),
                "n_pref": len(pref), "n_intro": intro_n, "heads": out_heads,
                "req_prof": profile(req), "req_sample": [r[:160] for r in req],
                "pref_lead": sum(bool(re.match(r"^\W*(?:preferred|desired|nice to have|bonus|plus)\b", r, re.I))
                                 for r in req)}
    except Exception as exc:  # noqa: BLE001
        return {"pid": pid, "employer": employer, "platform": platform, "error": f"{type(exc).__name__}: {exc}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--per-employer", type=int, default=40)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--population", action="store_true", help="the Jev/judge population instead of a corpus sample")
    a = ap.parse_args()
    con = duckdb.connect(os.path.expanduser("~/jobsearch/db/jobsearch.duckdb"), read_only=True)
    if a.population:
        rows = con.execute("""SELECT p.posting_id, p.employer, p.platform, p.description_text FROM vw_lens_fit f
            JOIN postings p USING (posting_id) WHERE p.status='active' AND f.verdict != 'reject'
            AND p.description_text IS NOT NULL""").fetchall()
    else:
        rows = con.execute("""SELECT posting_id, employer, platform, description_text FROM (
            SELECT *, row_number() OVER (PARTITION BY employer ORDER BY hash(posting_id)) rn FROM postings
            WHERE status='active' AND coalesce(track,'fit')='fit' AND length(coalesce(description_text,''))>200)
            WHERE rn <= ?""", [a.per_employer]).fetchall()
    con.close()
    print(f"{len(rows)} postings", file=sys.stderr)
    with Pool(a.workers) as pool, open(a.out, "w") as f:
        for i, rec in enumerate(pool.imap_unordered(study, rows, chunksize=8), 1):
            f.write(json.dumps(rec) + "\n")
            if i % 500 == 0:
                print(f"  {i}", file=sys.stderr)


if __name__ == "__main__":
    main()
