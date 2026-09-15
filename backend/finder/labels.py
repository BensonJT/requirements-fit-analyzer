"""Training labels for the fit model, read from the vault and the user's own decisions.

Sources (docs/SPRINT_PLAN.md section 8):
- application           Applications/*/index.md with a real `## Job Description`; label from frontmatter status.
- jobs_found_escalated  `# Company:` blocks in hand-made Jobs_Found files; label 1, weight 0.7.
- jobs_found_passed     `## Passed / Filtered Out` rows; label 0, text filled from a matching posting.
- pseudo_neg            random postings the rules reject on title; label 0, weight 0.5.
Decisions (build / pass) join these through `vw_label_set`.

Files written by the pipeline itself (`# Jobs Found — ATS pipeline`) are skipped: their blocks and
passed rows are the rules' output, and training on them would teach the model to echo the rules.
"""
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend import profile as P
from backend.screen import company_keys, company_matches, norm_company, similar_title

from . import rules
from .tracker_sync import job_search_dir

MIN_TEXT = 800
NEGATIVE_STATUSES = {"pass", "not-pursuing", "not_pursuing", "passed"}
PIPELINE_MARKER = "# Jobs Found — ATS pipeline"
TITLE_REJECT_REASONS = ("off-function title", "off-lane title")
SOURCES = ("application", "jobs_found_escalated", "jobs_found_passed", "pseudo_neg")

# A passed row says nothing about fit when the posting was simply gone, or was already tracked.
STALE_REASON = re.compile(r"\b(closed|expired|no longer (?:active|available|accepting|open)|not found|no active"
                          r"|already (?:applied|tracked|in (?:the )?tracker)|dedup|duplicate|could not (?:find|verify))\b", re.I)
# Location / pay only: the function may be a fit, so the row is not a text negative.
LOGISTICS_REASON = re.compile(r"\b(remote|hybrid|on-?site|in-office|in office|commut\w*|relocat\w*|days?/week"
                              r"|salary|comp\w*|pay|band|floor|below|\$\d)", re.I)
FIT_REASON = re.compile(r"\b(discipline|sales|revenue|gtm|junior|lane|function|scope|plant|manufactur\w*|domain"
                        r"|engineer\w*|coding|assessment|test|product|mismatch|not a fit|wrong|specializ\w*|clinical"
                        r"|practitioner|itil|technical|developer|audit|level|years|tenure|clearance|policy)", re.I)
# File-level sections that end an escalated block in hand-made files (blocks may run back to back).
_BLOCK_END = re.compile(r"^(---\s*$|# |## (Passed|Excluded|Already Applied|Watch|Coverage|Summary|Search Metadata"
                        r"|Run Notes|Notes|Priority|Fit Table|Shortlist|Verified|Executive|Best-fit|Escalated))")
_URL = re.compile(r"https?://[^\s)\]>|]+")


@dataclass
class LabelDoc:
    label_id: str
    source: str
    source_ref: Optional[str]
    company: Optional[str]
    title: Optional[str]
    text: Optional[str]
    label: int
    weight: float = 1.0
    posting_id: Optional[str] = None
    url: Optional[str] = None


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:20]


def strip_boilerplate(text: str) -> str:
    """Removes BOILERPLATE_PATTERNS matches line by line (EEO, benefits, about-us) and drops emptied lines."""
    pats = [re.compile(p, re.I) for p in P.BOILERPLATE_PATTERNS]
    out = []
    for line in (text or "").splitlines():
        for rx in pats:
            line = rx.sub("", line)
        if line.strip():
            out.append(line.rstrip())
    return "\n".join(out)


def _clean_md(value: str) -> str:
    """Cell / frontmatter text without bold markers, quotes or stray whitespace."""
    return re.sub(r"\s+", " ", (value or "").replace("**", "")).strip().strip("'\"").strip()


def _frontmatter(text: str) -> dict:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    out = {}
    for line in (m.group(1).splitlines() if m else []):
        if ":" in line and not line.startswith((" ", "\t", "-")):
            key, value = line.split(":", 1)
            value = _clean_md(value)
            out[key.strip().lower()] = None if value in ("", "~", "null") else value
    return out


def jd_section(text: str) -> str:
    """The `## Job Description` section, minus leading `Apply:` / ` · ` metadata lines before the first blank line."""
    m = re.search(r"^## Job Description[^\n]*\n(.*?)(?=^## (?!#)|\Z)", text, re.S | re.M)
    if not m:
        return ""
    lines = m.group(1).strip("\n").splitlines()
    head = []
    while lines and lines[0].strip():
        head.append(lines.pop(0))
    head = [h for h in head if not (h.strip().startswith("Apply:") or " · " in h)]
    return "\n".join(head + lines).strip()


def _first_url(text: str) -> Optional[str]:
    m = re.search(r"^\s*Apply:\s*.*?(https?://\S+)", text or "", re.M)
    return m.group(1).rstrip(").,]") if m else None


def load_applications(vault_dir: str) -> list:
    """One doc per application folder with a JD of at least MIN_TEXT characters."""
    docs = []
    for index in sorted((job_search_dir(vault_dir) / "Applications").glob("*/index.md")):
        raw = index.read_text(encoding="utf-8", errors="ignore")
        text = jd_section(raw)
        if len(text) < MIN_TEXT:
            continue
        fm = _frontmatter(raw)
        status = (fm.get("status") or "").lower()
        folder = index.parent.name
        docs.append(LabelDoc(_id("application", folder), "application", folder, fm.get("company") or folder,
                             fm.get("role") or folder, text, 0 if status in NEGATIVE_STATUSES else 1, 1.0,
                             url=_first_url(raw)))
    return docs


def _jobs_found_files(vault_dir: str) -> list:
    """Hand-made Jobs_Found files, newest first (pipeline-written files excluded)."""
    folder = job_search_dir(vault_dir) / "Search_Results"
    files = []
    for path in sorted(folder.glob("Jobs_Found_*.md"), reverse=True):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if PIPELINE_MARKER not in text:
            files.append((path, text))
    return files


def _same_job(company: str, title: str, docs: list) -> bool:
    keys = company_keys(company)
    return any(company_matches(norm_company(d.company or ""), keys) and similar_title(title, d.title or "")
               for d in docs)


def parse_escalated_blocks(text: str) -> list:
    """(company, title, url, body) for each `# Company:` block."""
    out, lines, i = [], text.splitlines(), 0
    while i < len(lines):
        m = re.match(r"^# Company:\s*(.+)$", lines[i])
        if not m:
            i += 1
            continue
        company, title, url, body = _clean_md(m.group(1)), "", None, []
        i += 1
        while i < len(lines) and not _BLOCK_END.match(lines[i]):
            line = lines[i]
            t = re.match(r"^##? Title:\s*(.+)$", line)
            if t and not title:
                title = _clean_md(t.group(1))
            elif line.startswith("Apply:") and url is None:
                u = _URL.search(line)
                url = u.group(0) if u else None
            else:
                body.append(line)
            i += 1
        out.append((company, title, url, "\n".join(body).strip()))
    return out


def load_jobs_found_escalated(vault_dir: str, applications: list) -> list:
    """Escalated blocks as positives (weight 0.7), deduped against applications and across files."""
    docs = []
    for path, text in _jobs_found_files(vault_dir):
        for company, title, url, body in parse_escalated_blocks(text):
            if not title or _same_job(company, title, applications) or _same_job(company, title, docs):
                continue
            docs.append(LabelDoc(_id("jobs_found_escalated", path.name, company, title), "jobs_found_escalated",
                                 path.name, company, title, body if len(body) >= MIN_TEXT else None, 1, 0.7, url=url))
    return docs


def classify_passed_reason(reason: str) -> str:
    """'stale' (posting gone / already tracked), 'logistics' (location or pay only), or 'fit'."""
    if STALE_REASON.search(reason or ""):
        return "stale"
    if LOGISTICS_REASON.search(reason or "") and not FIT_REASON.search(reason or ""):
        return "logistics"
    return "fit"


def parse_passed_rows(text: str) -> list:
    """(company, title, url, reason) rows of the `## Passed / Filtered Out` table(s)."""
    out = []
    for m in re.finditer(r"^## Passed / Filtered Out[^\n]*\n(.*?)(?=^#{1,2} |\Z)", text, re.S | re.M):
        header = None
        for line in m.group(1).splitlines():
            s = line.strip()
            if not s.startswith("|"):
                continue
            if set(s) <= set("|- :"):
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            if header is None:
                header = cells
                continue
            if len(cells) < 3 or _clean_md(cells[0]).lower() == "company":
                continue
            company, role, reason = _clean_md(cells[0]), cells[1], _clean_md(cells[-1])
            link = re.search(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", role)
            title = _clean_md(link.group(1) if link else role.split(",")[0])
            url = link.group(2) if link else (_URL.search(role).group(0) if _URL.search(role) else None)
            if company and title and title.lower() not in ("various", "—", "-", "n/a"):
                out.append((company, title, url, reason))
    return out


def load_jobs_found_passed(vault_dir: str) -> tuple:
    """(docs, skipped Counter). Fit-reason passed rows as negatives with text NULL until matched."""
    docs, skipped, seen = [], Counter(), set()
    for path, text in _jobs_found_files(vault_dir):
        for company, title, url, reason in parse_passed_rows(text):
            kind = classify_passed_reason(reason)
            if kind != "fit":
                skipped[kind] += 1
                continue
            key = (norm_company(company), title.lower())
            if key in seen:
                skipped["duplicate"] += 1
                continue
            seen.add(key)
            docs.append(LabelDoc(_id("jobs_found_passed", path.name, company, title), "jobs_found_passed",
                                 f"{path.name}: {reason[:120]}", company, title, None, 0, 1.0, url=url))
    return docs, skipped


class PostingIndex:
    """Matches a (company, title, url) to a posting: exact URL, then the employer's req_id inside the
    URL, then company keys + similar title (newest first_seen_at wins). Active and closed rows."""

    def __init__(self, con):
        rows = con.execute("SELECT posting_id, employer, title, url, req_id, first_seen_at FROM postings").fetchall()
        self.by_url = {r[3]: r[0] for r in rows if r[3]}
        self.by_employer = defaultdict(list)
        for pid, employer, title, url, req_id, first_seen in rows:
            self.by_employer[employer].append((pid, title or "", req_id or "", first_seen))
        self.employer_keys = {e: norm_company(e) for e in self.by_employer}

    def match(self, company: str, title: str, url: Optional[str] = None) -> Optional[str]:
        if url and url in self.by_url:
            return self.by_url[url]
        keys = company_keys(company or "")
        employers = [e for e, k in self.employer_keys.items() if company_matches(k, keys)]
        if url:
            for e in employers:
                hits = [(fs, pid) for pid, _, req, fs in self.by_employer[e]
                        if len(req) >= 4 and re.search(rf"(?<![A-Za-z0-9]){re.escape(req)}(?![0-9])", url)]
                if hits:
                    return max(hits, key=lambda h: (h[0] is not None, h[0]))[1]
        cands = [(fs, pid) for e in employers for pid, t, _, fs in self.by_employer[e] if similar_title(title or "", t)]
        return max(cands, key=lambda h: (h[0] is not None, h[0]))[1] if cands else None


def match_to_postings(con, docs: list, index: Optional[PostingIndex] = None) -> int:
    """Fills posting_id on every doc it can, and text from the posting when the doc has none. Returns matches."""
    index = index or PostingIndex(con)
    matched = []
    for d in docs:
        if not d.posting_id:
            d.posting_id = index.match(d.company, d.title, d.url)
        if d.posting_id:
            matched.append(d)
    need_text = [d for d in matched if not d.text]
    if need_text:
        ids = json.dumps(sorted({d.posting_id for d in need_text}))
        texts = dict(con.execute("SELECT posting_id, description_text FROM postings WHERE posting_id IN "
                                 "(SELECT unnest(json_transform(?, '[\"VARCHAR\"]')))", [ids]).fetchall())
        for d in need_text:
            t = texts.get(d.posting_id) or ""
            d.text = t if len(t) >= MIN_TEXT else None
    return len(matched)


def pseudo_negatives(con, n: int = 1500, seed: int = 7, exclude: Optional[set] = None) -> list:
    """Random active postings with a JD that the current rules reject on an off-function / off-lane title."""
    exclude = exclude or set()
    docs, offset, page = [], 0, max(n * 4, 200)
    cols = ("posting_id", "employer", "platform", "req_id", "title", "url", "location_primary",
                         "locations", "country", "workplace_type", "employment_type", "job_level", "pay_min",
                         "pay_max", "pay_interval", "description_text")
    while len(docs) < n:
        batch = con.execute(f"""
            SELECT {', '.join(cols)} FROM postings
            WHERE status = 'active' AND length(description_text) >= {MIN_TEXT}
            ORDER BY hash(posting_id || ?), posting_id LIMIT ? OFFSET ?""", [str(seed), page, offset]).fetchall()
        if not batch:
            break
        offset += page
        for values in batch:
            row = dict(zip(cols, values))
            if row["posting_id"] in exclude:
                continue
            rec = rules.screen_row(row)
            if rec.verdict == "reject" and any(r.startswith(TITLE_REJECT_REASONS) for r in rec.reasons):
                docs.append(LabelDoc(_id("pseudo_neg", row["posting_id"]), "pseudo_neg", None, row["employer"],
                                     row["title"], row["description_text"], 0, 0.5, posting_id=row["posting_id"]))
                if len(docs) >= n:
                    break
    return docs


def collect(con, vault_dir: str, n_pseudo: int = 1500, seed: int = 7) -> tuple:
    """(docs, stats) from every source, with posting ids and missing text filled."""
    apps = load_applications(vault_dir)
    escalated = load_jobs_found_escalated(vault_dir, apps)
    passed, skipped = load_jobs_found_passed(vault_dir)
    index = PostingIndex(con)
    for group in (apps, escalated, passed):
        match_to_postings(con, group, index)
    positives = {d.posting_id for d in apps + escalated if d.posting_id and d.label == 1}
    positives |= {pid for (pid,) in con.execute("SELECT posting_id FROM vw_decisions WHERE decision = 'build'").fetchall()}
    pseudo = pseudo_negatives(con, n=n_pseudo, seed=seed, exclude=positives) if n_pseudo else []
    return apps + escalated + passed + pseudo, {"passed_skipped": dict(skipped)}


def sync_labels(con, vault_dir: str, n_pseudo: int = 1500, seed: int = 7, log=print) -> dict:
    """Rebuilds label_docs from the vault (one transaction) and logs counts per source."""
    docs, stats = collect(con, vault_dir, n_pseudo=n_pseudo, seed=seed)
    now = _now()
    payload = json.dumps([{k: v for k, v in asdict(d).items() if k != "url"} for d in docs])
    shape = json.dumps([{"label_id": "VARCHAR", "source": "VARCHAR", "source_ref": "VARCHAR", "company": "VARCHAR",
                         "title": "VARCHAR", "text": "VARCHAR", "label": "INTEGER", "weight": "DOUBLE",
                         "posting_id": "VARCHAR"}])
    con.execute("BEGIN")
    try:
        con.execute("DELETE FROM label_docs WHERE source IN (SELECT unnest(?::VARCHAR[]))", [list(SOURCES)])
        if docs:
            con.execute("""
                INSERT OR REPLACE INTO label_docs (label_id, source, source_ref, posting_id, company, title, text,
                                                   label, weight, loaded_at)
                SELECT label_id, source, source_ref, posting_id, company, title, text, label, weight, $3
                FROM (SELECT unnest(json_transform($1, $2), recursive := true))""", [payload, shape, now])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    counts = label_counts(con)
    counts.update(stats)
    for (source, label), c in sorted(counts["by_source"].items()):
        log(f"  {source:<22} label {label}: {c['docs']:>5} docs, {c['with_text']:>5} with text, "
            f"{c['matched']:>4} matched to a posting")
    log(f"Labels: {counts['pos_text']} positives with text, {counts['neg_text']} non-pseudo negatives with text, "
        f"{counts['pseudo_text']} pseudo-negatives; passed rows skipped {stats['passed_skipped']}")
    return counts


def label_counts(con) -> dict:
    """Counts per (source, label) in label_docs plus the vw_label_set totals training will see."""
    by_source = {}
    for source, label, docs, with_text, matched in con.execute("""
            SELECT source, label, count(*), count(text), count(posting_id) FROM label_docs GROUP BY 1, 2""").fetchall():
        by_source[(source, label)] = {"docs": docs, "with_text": with_text, "matched": matched}
    for source, label, docs in con.execute("SELECT source, label, count(*) FROM vw_label_set "
                                           "WHERE source = 'decision' GROUP BY 1, 2").fetchall():
        by_source[(source, label)] = {"docs": docs, "with_text": docs, "matched": docs}
    pos = sum(c["with_text"] for (s, l), c in by_source.items() if l == 1)
    neg = sum(c["with_text"] for (s, l), c in by_source.items() if l == 0 and s != "pseudo_neg")
    pseudo = by_source.get(("pseudo_neg", 0), {}).get("with_text", 0)
    return {"by_source": by_source, "pos_text": pos, "neg_text": neg, "pseudo_text": pseudo}
