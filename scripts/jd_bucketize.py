"""Extract a JD's resume keywords and bucket them: 1 = embed in the resume, 2 = noise, 3 = domain claims to avoid.

Usage (from ~/jobsearch):
    .venv/bin/python scripts/jd_bucketize.py <index.md or jd.txt> [--resume Resume_Tailored.md] [--output CSV]
                                             [--no-reclassify] [--raw] [--full-text] [--provider aistudio|groq]

Steps:
    0. Keep only the JD's duties and requirements (the finder's section splitter): EEO, pay, benefits and the
       company overview are dropped before extraction. --full-text skips this.
    1. Extract keywords with spaCy and the skill taxonomy (backend/jd_keywords/extract.py; no API call).
    2. Bucket them with an LLM (backend/jd_keywords/classify.py): Gemma 4 on Google AI Studio by default, Groq as
       the fallback (--provider groq or JD_BUCKETIZE_PROVIDER=groq). Calls are paced by tokens per minute.
    3. Write keywords_bucket1.csv (phrase, bucket, skill_type, jd_count) beside the JD source, or to --output.
    4. With --resume: report exact-match coverage of the Bucket 1 phrases.

Flags:
    --no-reclassify  Skip steps 0-3; read the existing CSV (keeps hand edits) and score --resume against it.
    --raw            Skip step 2 and put every extracted keyword in Bucket 1 (diagnostics).

Environment (repo .env): GEMINI_API_KEY (shared with judge2), GROQ_API_KEY, LLM_GROQ_MODEL; optional
JD_BUCKETIZE_MODEL, JD_BUCKETIZE_BATCH_SIZE, JD_BUCKETIZE_TPM, JD_BUCKETIZE_MAX_TOKENS, JD_BUCKETIZE_TIMEOUT.
Moved from resume_ide_v2 on 2026-10-30 (that project is retired).
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env", override=False)

from backend.jd_keywords import classify, extract  # noqa: E402


def say(*args) -> None:
    print(*args, flush=True)


def extract_jd_text(source: Path) -> str:
    """An application index.md: only its '## Job Description' section. Any other file: the whole file."""
    raw = source.read_text(encoding="utf-8")
    if source.name.lower() != "index.md":
        return raw
    jd_lines, in_jd = [], False
    for line in raw.splitlines():
        if re.match(r"^##\s+Job Description", line, re.IGNORECASE):
            in_jd = True
            continue
        if in_jd:
            if re.match(r"^##\s+", line):
                break
            jd_lines.append(line)
    return "\n".join(jd_lines).strip() if jd_lines else raw


def _kw_pattern(kw_lower: str) -> re.Pattern:
    """Whole-word match that also handles C++, .NET and C# edges."""
    start = r"\b" if re.match(r"\w", kw_lower) else r"(?<!\w)"
    end = r"\b" if re.search(r"\w$", kw_lower) else r"(?!\w)"
    return re.compile(start + re.escape(kw_lower) + end, re.IGNORECASE)


def score_resume(resume_text: str, bucket1: list[str]) -> dict:
    matched = [p for p in bucket1 if _kw_pattern(p.lower()).search(resume_text)]
    unmatched = [p for p in bucket1 if p not in matched]
    pct = round(len(matched) / len(bucket1) * 100, 1) if bucket1 else 0.0
    return {"matched": matched, "unmatched": unmatched, "total": len(bucket1), "coverage_pct": pct}


def write_csv(path: Path, buckets: dict[str, list[str]], meta: dict[str, dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["phrase", "bucket", "skill_type", "jd_count"])
        w.writeheader()
        for num, key in (("1", "bucket1"), ("2", "bucket2"), ("3", "bucket3")):
            for phrase in buckets[key]:
                m = meta.get(phrase.lower(), {})
                w.writerow({"phrase": phrase, "bucket": num,
                            "skill_type": m.get("skill_type", "" if num == "2" else "hard"),
                            "jd_count": m.get("count", "")})


def read_csv(path: Path) -> dict[str, list[str]]:
    out = {"bucket1": [], "bucket2": [], "bucket3": []}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            phrase, b = (row.get("phrase") or "").strip(), (row.get("bucket") or "").strip()
            if phrase and b in ("1", "2", "3"):
                out[f"bucket{b}"].append(phrase)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract and bucket JD keywords for ATS-aware resume writing.")
    ap.add_argument("jd_source", help="application index.md or a plain JD file")
    ap.add_argument("--resume", metavar="PATH", help="Resume_Tailored.md: report Bucket 1 coverage")
    ap.add_argument("--output", metavar="CSV", help="default: keywords_bucket1.csv beside jd_source")
    ap.add_argument("--no-reclassify", action="store_true", help="score --resume against the existing CSV")
    ap.add_argument("--raw", action="store_true", help="no LLM: every extracted keyword goes to Bucket 1")
    ap.add_argument("--full-text", action="store_true", help="extract from the whole JD, boilerplate included")
    ap.add_argument("--provider", choices=sorted(classify.PROVIDER_DEFAULTS), help="override JD_BUCKETIZE_PROVIDER")
    args = ap.parse_args()

    source = Path(args.jd_source).resolve()
    if not source.exists():
        sys.exit(f"ERROR: JD source not found: {source}")
    output = Path(args.output).resolve() if args.output else source.parent / "keywords_bucket1.csv"
    say(f"\nSource:  {source}\nOutput:  {output}\n")

    if args.no_reclassify:
        if not output.exists():
            sys.exit(f"ERROR: --no-reclassify needs an existing CSV at {output}")
        buckets = read_csv(output)
        say(f"Read existing CSV: {len(buckets['bucket1'])} / {len(buckets['bucket2'])} / "
            f"{len(buckets['bucket3'])} phrases in buckets 1 / 2 / 3 (unchanged)")
    else:
        jd_text = extract_jd_text(source)
        if not jd_text.strip():
            sys.exit("ERROR: no JD text found (index.md needs a '## Job Description' section).")
        say(f"Step 0: JD text {len(jd_text):,} characters")
        if not args.full_text:
            jd_text, sizes = extract.duties_and_requirements(jd_text)
            if sizes:
                kept = sum(v for k, v in sizes.items() if k in extract.KEEP_SECTIONS)
                parts = ", ".join(f"{k} {v:,}" for k, v in sorted(sizes.items(), key=lambda x: -x[1]))
                say(f"  kept duties and requirements: {kept:,} characters ({parts}); boilerplate dropped")
            else:
                say("  splitter found no duties or requirements; using the full text")
        say("Step 1: extracting keywords (spaCy + taxonomy)...")
        keywords = extract.extract_keywords(jd_text)
        say(f"  {len(keywords)} candidate keywords after the quality filter")
        meta = {kw["keyword"].lower(): kw for kw in keywords}
        if args.raw:
            say("Step 2: skipped (--raw)")
            buckets = {"bucket1": [kw["keyword"] for kw in keywords], "bucket2": [], "bucket3": []}
        else:
            say("Step 2: bucketing with the LLM...")
            buckets = classify.bucketize(keywords, provider=args.provider, log=say)
        write_csv(output, buckets, meta)
        say(f"\nBucket 1 (embed): {len(buckets['bucket1'])}  |  Bucket 2 (noise): {len(buckets['bucket2'])}  |  "
            f"Bucket 3 (skip): {len(buckets['bucket3'])}  ->  {output}")
        if buckets["bucket1"]:
            say("\nBucket 1 phrases:")
            for p in sorted(buckets["bucket1"], key=lambda p: -len(p)):
                say(f"  - {p}")
        if buckets["bucket3"]:
            say("\nBucket 3 phrases (domain claims to avoid):")
            for p in buckets["bucket3"]:
                say(f"  - {p}")

    if args.resume:
        resume = Path(args.resume).resolve()
        if not resume.exists():
            say(f"\nWARNING: resume not found: {resume}; coverage skipped.")
        else:
            s = score_resume(resume.read_text(encoding="utf-8"), buckets["bucket1"])
            say(f"\nCoverage (exact match): {len(s['matched'])}/{s['total']} Bucket 1 phrases = {s['coverage_pct']}%")
            if s["unmatched"]:
                say("Uncovered:")
                for p in s["unmatched"]:
                    say(f"  - {p}")
    say("")


if __name__ == "__main__":
    main()
