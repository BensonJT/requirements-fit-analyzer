"""Profile-driven job sweep: harvest -> screen -> dedup -> triage report.

Replaces the March 2026 keyword list (Chief of Staff / Workforce Planning / Director of
Operations) with the function list in backend/profile.py, and adds
the profile's hard rules as a pre-screen. Output is a shortlist to VERIFY, not a verdict:
every candidate still has to be opened on the employer's own posting before it goes into
a Jobs_Found file.

Usage:
    python sweep.py                      # 7 days, all sources
    python sweep.py --days 14 --sources adzuna,usajobs
"""
import argparse
import csv
import glob
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta

import httpx
from dotenv import load_dotenv

from backend import profile as P
from backend.screen import Listing, company_keys, norm_company, norm_title, parse_salary_text, screen

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
# Optional: a directory holding Tools/tracker_lookup.py and Search_Results/Jobs_Found_*.md,
# used only to skip jobs already applied to or already escalated. Unset = no dedup.
JOB_SEARCH = os.path.expanduser(os.getenv("JOBSEARCH_VAULT_DIR", ""))


# ---------------------------------------------------------------- sources

def adzuna(client, phrases, days, log):
    app_id, app_key = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    if not app_id:
        log.append("Adzuna: credentials missing")
        return []
    base = "https://api.adzuna.com/v1/api/jobs/us/search"
    out = []

    def call(page, params, pass_name, local):
        params = {"app_id": app_id, "app_key": app_key, "results_per_page": 50,
                  "max_days_old": days, **params}
        for attempt in range(3):
            r = client.get(f"{base}/{page}", params=params)
            if r.status_code == 429:
                time.sleep(20)
                continue
            break
        if r.status_code != 200:
            log.append(f"Adzuna {pass_name}: HTTP {r.status_code}")
            return 0
        d = r.json()
        for j in d.get("results", []):
            out.append(Listing(
                source="Adzuna", search_pass=pass_name, title=j.get("title", ""),
                company=j.get("company", {}).get("display_name", "Unknown"),
                location=j.get("location", {}).get("display_name", ""),
                # /land/ad/ redirects to the source posting -- resolve_links() follows it
                url=f"https://www.adzuna.com/land/ad/{j.get('id')}",
                posted_at=(j.get("created") or "")[:10], description=j.get("description", ""),
                salary_min=j.get("salary_min"), salary_max=j.get("salary_max"),
                salary_predicted=str(j.get("salary_is_predicted")) == "1",
                is_local_pass=local, extra={"contract_type": j.get("contract_type", "")},
            ))
        time.sleep(2.6)  # Adzuna free tier is ~25 calls/minute
        return d.get("count", 0)

    for phrase in phrases:
        # Nationwide, phrase in TITLE -- the remote lane. title_only kills the Banquet Captain noise.
        n = call(1, {"title_only": phrase, "sort_by": "date"}, f"adzuna:title:{phrase}", False)
        if n > 50:
            call(2, {"title_only": phrase, "sort_by": "date"}, f"adzuna:title:{phrase}", False)
        # Local lane -- phrase anywhere, inside the commute radius (needs HOME in profile_local.py).
        if P.HOME:
            call(1, {"what_phrase": phrase, "where": P.HOME, "distance": P.LOCAL_RADIUS_KM},
                 f"adzuna:local:{phrase}", True)
    return out


def jooble(client, phrases, days, log):
    key = os.getenv("JOOBLE_API_KEY")
    if not key:
        log.append("Jooble: key missing")
        return []
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    out = []
    for phrase in phrases:
        for loc, local in (("Remote", False),) + (((P.HOME, True),) if P.HOME else ()):
            body = {"keywords": phrase, "location": loc, "page": "1", "ResultOnPage": "50",
                    "datecreatedfrom": since}
            if local:
                body["radius"] = str(P.LOCAL_RADIUS_KM)
            r = client.post(f"https://jooble.org/api/{key}", json=body)
            if r.status_code != 200:
                log.append(f"Jooble {phrase}/{loc}: HTTP {r.status_code}")
                continue
            for j in r.json().get("jobs", []):
                lo, hi, _ = parse_salary_text(j.get("salary", ""))
                out.append(Listing(
                    source="Jooble", search_pass=f"jooble:{'local' if local else 'remote'}:{phrase}",
                    title=j.get("title", ""), company=j.get("company", "") or "Unknown",
                    location=j.get("location", ""), url=j.get("link", ""),
                    posted_at=(j.get("updated") or "")[:10],
                    description=re.sub(r"<[^>]+>|&nbsp;", " ", j.get("snippet", "")),
                    salary_min=lo, salary_max=hi, salary_text=j.get("salary", ""),
                    is_local_pass=local, extra={"via": j.get("source", "")},
                ))
    return out


def usajobs(client, phrases, days, log):
    key, email = os.getenv("USAJOBS_API_KEY"), os.getenv("USAJOBS_EMAIL")
    if not key:
        log.append("USAJobs: credentials missing")
        return []
    headers = {"Host": "data.usajobs.gov", "User-Agent": email, "Authorization-Key": key}
    out = []
    for phrase in phrases:
        lanes = [({"RemoteIndicator": "True"}, False)]
        if P.HOME:
            lanes.append(({"LocationName": P.HOME, "Radius": round(P.LOCAL_RADIUS_KM / 1.609)}, True))
        for extra, local in lanes:
            params = {"Keyword": phrase, "ResultsPerPage": 100, "DatePosted": min(days, 60), **extra}
            r = client.get("https://data.usajobs.gov/api/search", params=params, headers=headers)
            if r.status_code != 200:
                log.append(f"USAJobs {phrase}: HTTP {r.status_code}")
                continue
            for item in r.json().get("SearchResult", {}).get("SearchResultItems", []):
                m = item.get("MatchedObjectDescriptor", {})
                pay = (m.get("PositionRemuneration") or [{}])[0]
                details = m.get("UserArea", {}).get("Details", {})
                lo, hi = pay.get("MinimumRange"), pay.get("MaximumRange")
                rate = pay.get("RateIntervalCode", "PA")
                out.append(Listing(
                    source="USAJobs", search_pass=f"usajobs:{'local' if local else 'remote'}:{phrase}",
                    title=m.get("PositionTitle", ""), company=m.get("OrganizationName", ""),
                    location=m.get("PositionLocationDisplay", ""), url=m.get("PositionURI", ""),
                    posted_at=(m.get("PublicationStartDate") or "")[:10],
                    description=" ".join([m.get("QualificationSummary", ""),
                                          " ".join(details.get("MajorDuties", []) or [])]),
                    salary_min=float(lo) if lo and rate == "PA" else None,
                    salary_max=float(hi) if hi and rate == "PA" else None,
                    is_local_pass=local or bool(details.get("RemoteIndicator")),
                    extra={"closes": (m.get("ApplicationCloseDate") or "")[:10],
                           "remote": details.get("RemoteIndicator"),
                           "grade": f"{(m.get('JobGrade') or [{}])[0].get('Code', '')}-{details.get('LowGrade', '')}/{details.get('HighGrade', '')}",
                           "hiring_path": ",".join(details.get("HiringPath", []) or [])},
                ))
                if details.get("RemoteIndicator"):
                    out[-1].location += " (Remote)"
    return out


SOURCES = {"adzuna": adzuna, "jooble": jooble, "usajobs": usajobs}


# ---------------------------------------------------------------- dedup inputs

def load_tracker():
    script = os.path.join(JOB_SEARCH, "Tools/tracker_lookup.py")
    if not JOB_SEARCH or not os.path.exists(script):
        return []
    res = subprocess.run([sys.executable, script], capture_output=True, text=True)
    rows = []
    for line in res.stdout.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 4:
            rows.append({"section": parts[0], "date": parts[1], "company": parts[2],
                         "company_keys": company_keys(parts[2]), "role": " | ".join(parts[3:])})
    return rows


def load_recent_jobs_found(days=10):
    """(company, title) pairs already escalated in recent Jobs_Found files."""
    seen = {}
    cutoff = datetime.now() - timedelta(days=days)
    for path in (sorted(glob.glob(os.path.join(JOB_SEARCH, "Search_Results/Jobs_Found_*.md"))) if JOB_SEARCH else []):
        if datetime.fromtimestamp(os.path.getmtime(path)) < cutoff:
            continue
        text = open(path, encoding="utf-8", errors="ignore").read()
        for co, ti in re.findall(r"^# Company:\s*(.+)\n## Title:\s*(.+)$", text, re.M):
            seen[(norm_company(co), norm_title(ti))] = os.path.basename(path)
    return seen


# ---------------------------------------------------------------- pipeline

DEAD_MARKERS = ["no longer accepting applications", "this job has expired", "job is no longer available",
                "position has been filled", "no longer available", "job has been closed", "posting has closed"]


def resolve_links(jobs, workers=8):
    """Follow each shortlisted link to its landing page; record the final URL and dead-posting signals.

    A live landing page is NOT proof the requisition is open -- it only removes the obvious corpses
    and hands over the employer URL for the manual read.
    """
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}

    def one(job):
        try:
            with httpx.Client(timeout=20, follow_redirects=True, headers=headers) as c:
                r = c.get(job.url)
            job.extra["final_url"] = str(r.url)
            job.extra["http"] = r.status_code
            body = r.text[:200_000].lower()
            if r.status_code in (404, 410) or any(m in body for m in DEAD_MARKERS):
                job.flags.append(f"landing page looks dead (HTTP {r.status_code})")
        except Exception as e:
            job.extra["http"] = f"error {type(e).__name__}"

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(one, jobs))

def collapse(listings):
    """Merge the same company+title posted in many cities into one row (keeps all locations)."""
    groups = defaultdict(list)
    for j in listings:
        groups[(norm_company(j.company), norm_title(j.title))].append(j)
    merged = []
    for rows in groups.values():
        best = max(rows, key=lambda j: (not j.salary_predicted and bool(j.salary_max), len(j.description)))
        best.locations = sorted({r.location for r in rows if r.location})
        best.is_local_pass = any(r.is_local_pass for r in rows)
        best.extra["passes"] = sorted({r.search_pass for r in rows})
        best.extra["sources"] = sorted({r.source for r in rows})
        merged.append(best)
    return merged


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--sources", default="adzuna,jooble,usajobs")
    ap.add_argument("--outdir", default="output")
    ap.add_argument("--from-raw", help="re-screen a saved raw_*.json instead of calling the APIs")
    ap.add_argument("--no-resolve", action="store_true", help="skip following shortlist links")
    args = ap.parse_args()

    load_dotenv()
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    log = []
    phrases = P.FUNCTION_PHRASES + P.SEPARATE_PASS_PHRASES
    os.makedirs(args.outdir, exist_ok=True)

    if args.from_raw:
        raw = [Listing(**d) for d in json.load(open(args.from_raw))]
        log.append(f"re-screened {len(raw)} raw rows from {args.from_raw}")
    else:
        raw = []
        with httpx.Client(timeout=40) as client:
            for name in args.sources.split(","):
                t0 = time.time()
                try:
                    got = SOURCES[name](client, phrases, args.days, log)
                except Exception as e:  # one source failing must not kill the sweep
                    log.append(f"{name}: FAILED {e!r}")
                    got = []
                log.append(f"{name}: {len(got)} raw rows in {time.time() - t0:.0f}s")
                raw.extend(got)
        # Save the harvest so screening rules can be tuned without spending API quota again.
        with open(os.path.join(args.outdir, f"raw_{stamp}.json"), "w") as f:
            json.dump([asdict(j) for j in raw], f)

    tracker = load_tracker()
    recent = load_recent_jobs_found()
    jobs = [screen(j, tracker, recent) for j in collapse(raw)]
    shortlist = [j for j in jobs if j.verdict != "reject"]
    if not args.no_resolve:
        resolve_links(shortlist)
        for j in shortlist:
            if j.flags and j.verdict == "candidate":
                j.verdict = "review"
    jobs.sort(key=lambda j: ({"candidate": 0, "review": 1, "reject": 2}[j.verdict], j.extra.get("tier", 2), -j.score))
    csv_path = os.path.join(args.outdir, f"sweep_{stamp}.csv")
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["verdict", "score", "title", "company", "location", "all_locations", "annual_top",
                    "salary_predicted", "posted", "source", "passes", "url", "final_url", "http",
                    "reasons", "flags"])
        for j in jobs:
            w.writerow([j.verdict, j.score, j.title, j.company, j.location, "; ".join(j.locations),
                        j.annual_top or "", j.salary_predicted, j.posted_at, j.source,
                        "; ".join(j.extra.get("passes", [])), j.url, j.extra.get("final_url", ""),
                        j.extra.get("http", ""), "; ".join(j.reasons), "; ".join(j.flags)])

    md_path = os.path.join(args.outdir, f"sweep_{stamp}.md")
    counts = Counter(j.verdict for j in jobs)
    reason_counts = Counter(r.split(" (")[0].split(":")[0] for j in jobs for r in j.reasons)
    with open(md_path, "w") as f:
        f.write(f"# Sweep triage {stamp}\n\n")
        f.write(f"Window: last {args.days} days. Raw rows: {len(raw)}. After collapsing multi-city reposts: {len(jobs)}.\n\n")
        f.write(f"Candidates: {counts['candidate']} · Review: {counts['review']} · Rejected: {counts['reject']}\n\n")
        f.write("**Pre-screen only. Open every row on the employer's own posting before it goes into Jobs_Found.**\n\n")
        for verdict in ("candidate", "review"):
            f.write(f"## {verdict.title()}\n\n| Tier | Score | Title | Company | Location | Top | Source | Flags |\n|---|---|---|---|---|---|---|---|\n")
            for j in (x for x in jobs if x.verdict == verdict):
                top = f"${j.annual_top:,.0f}" if j.annual_top else "n/p"
                locs = j.location if len(j.locations) <= 1 else f"{j.location} (+{len(j.locations) - 1})"
                f.write(f"| {j.extra.get('tier', 2)} | {j.score} | [{j.title}]({j.url}) | {j.company} | {locs} | {top} | {j.source} | {'; '.join(j.flags)} |\n")
            f.write("\n")
        # A card-level location reject is unsafe (multi-location reqs), so show the strongest ones.
        loc_only = [j for j in jobs if j.verdict == "reject" and len(j.reasons) == 1
                    and j.reasons[0].startswith("not remote") and j.score >= 15]
        f.write("## Rejected on listing location only -- strongest 30 (a card can hide other locations)\n\n"
                "| Score | Title | Company | Location | Top |\n|---|---|---|---|---|\n")
        for j in sorted(loc_only, key=lambda j: -j.score)[:30]:
            top = f"${j.annual_top:,.0f}" if j.annual_top else "n/p"
            f.write(f"| {j.score} | [{j.title}]({j.url}) | {j.company} | {j.location} | {top} |\n")
        f.write("\n## Rejection reasons\n\n| Reason | Count |\n|---|---|\n")
        for r, n in reason_counts.most_common():
            f.write(f"| {r} | {n} |\n")
        f.write("\n## Run log\n\n" + "\n".join(f"- {line}" for line in log) + "\n")

    print("\n".join(log))
    print(f"candidates={counts['candidate']} review={counts['review']} reject={counts['reject']}")
    print(csv_path)
    print(md_path)


if __name__ == "__main__":
    main()
