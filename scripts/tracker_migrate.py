"""Migrate the hand-edited markdown application tracker into PostgreSQL (schema `tracker`).

    python scripts/tracker_migrate.py --tracker PATH/Application_Tracker.md --duckdb db/jobsearch.duckdb \
        --report REPORT.md [--db NAME] [--dry-run] [--force]

Design: DESIGN_tracker_db.md section 10. Re-runnable: it empties the tracker tables (not `tracker.status`)
and reloads, and refuses when any event has an actor other than `migration` unless --force. --dry-run parses
and maps everything, writes the report and never connects to PostgreSQL. The tracker file's directory is the
Job_Search directory; application folders are stored relative to it (`Applications/<name>`).

DuckDB is opened read-only. `tracker.matched_posting_id` counts as a posting id only when `match_kind` is
'exact' (the values in the table are exact / fuzzy / none); a fuzzy match is listed in the report as a
candidate and never filled.
"""
import argparse
import collections
import datetime as dt
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.finder.tracker_sync import split_cells  # noqa: E402
from backend.screen import company_keys, company_matches, norm_company  # noqa: E402

ACTOR = "migration"
HEX20 = re.compile(r"[0-9a-f]{20}")
EXACT_KINDS = ("exact",)          # DuckDB tracker.match_kind values that may fill a posting_id

# ---------------------------------------------------------------- status mapping
# Ordered (regex, status, confident) rules over the lower-cased status cell; the first match wins.
# `confident=False` rules are listed in the report for review. A status that is active but sits in the
# Closed section is converted by SECTION_CLOSED_FALLBACK (the section as tiebreaker), also low confidence.
STATUS_RULES = [
    (r"assumed[- ]?reject", "assumed-rejected", True),
    (r"\bdeclin", "declined", True),
    (r"withdr[ae]w", "withdrawn", True),
    (r"reject|not selected|unsuccessful|no longer (being )?considered", "rejected", True),
    (r"no longer (advertised|available|posted|taking|hiring|open)|post removed|not found|not open|missed"
     r"|closed before|not applied: closed|didn.?t finish|not (yet )?submitted", "closed-unsent", True),
    (r"interview|panel|final round|onsite", "interview", True),
    (r"passed .*screen|screen(ing)? (complete|passed)|hr screen|recruiter (call|screen)|hm review"
     r"|hiring manager", "screen", True),
    (r"\bon hold\b", "passed", True),     # user ruling: status text "On Hold" = decided not to apply
    (r"follow-?up q|inbound recruiter", "screen", False),
    (r"not pursu|\bskip|\bpass(ed)?\b|not interested|not built|not applied", "passed", True),
    (r"\brtr\b", "applied", True),       # user ruling: RTR unsigned stays applied
    (r"package|materials ready|unsent|evaluating|on hold|hold|ready to apply|site error|pending", "built", True),
    (r"appl(y|ied|ication)|submitted|\bsent\b|under review|awaiting", "applied", True),
]
_RULES = [(re.compile(p, re.I), s, c) for p, s, c in STATUS_RULES]
SECTION_CLOSED_FALLBACK = {"built": "closed-unsent", "applied": "assumed-rejected",
                           "screen": "rejected", "interview": "rejected"}
ACTIVE = {"built", "applied", "screen", "interview", "offer"}


def clean_text(s):
    s = re.sub(r"<br\s*/?>", " ", s or "", flags=re.I)
    s = s.replace("**", "").replace("\\|", "|")
    return re.sub(r"\s+", " ", s).strip()


def map_status(text, section, has_date_applied):
    """-> (status, confident, note). `note` says why a row is low-confidence or unmapped."""
    t = clean_text(text)
    sect = section.strip().lower()
    for rx, status, confident in _RULES:
        if rx.search(t):
            if sect == "closed" and status in SECTION_CLOSED_FALLBACK:
                return SECTION_CLOSED_FALLBACK[status], True, ""      # user-confirmed section tiebreaker
            return status, confident, "" if confident else "weak text rule"
    if sect == "closed":
        return "rejected", False, "unmatched; Closed default"
    return ("applied" if has_date_applied else "built"), False, "unmatched; Active default"


# ---------------------------------------------------------------- parsing helpers
def parse_date(s):
    s = (s or "").strip()
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    try:
        if m:
            return dt.date(int(m[1]), int(m[2]), int(m[3]))
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
        if m:
            return dt.date(int(m[3]), int(m[1]), int(m[2]))
    except ValueError:
        return None
    return None


_LOC = re.compile(r"remote|hybrid|on-?site|work at home|\b[A-Z]{2}\b|united states|\bUSA?\b|multi-state"
                  r"|multiple locations|\bnationwide\b", re.I)
_REQ_LABELLED = re.compile(r"(?:^|\s)(?:req(?:uisition)?|job ?id|job|id)\s*#?:?\s*([A-Za-z0-9][A-Za-z0-9_-]{2,})$", re.I)
_REQ_PLATFORM = re.compile(r"^(?:workday|greenhouse|lever|ashby|deel|taleo|icims)\s+([A-Za-z0-9_-]{3,})$", re.I)
_REQ_BARE = re.compile(r"^[A-Za-z]{0,3}-?\d{4,}[A-Za-z0-9-]*$")
_CLOSES = re.compile(r"(?:closes?|apply (?:by|before)|deadline)\s+(\d{4}-\d{2}-\d{2})", re.I)
_PAY = re.compile(r"\$\s?\d")
_PAY_NOISE = re.compile(r"^pay not (posted|listed|disclosed)$", re.I)
_REQ_ANYWHERE = re.compile(r"\b(?:req(?:uisition)?\.?|job)(?:\s+id)?(?:\s+|\s*[#:]\s*)([A-Za-z0-9_-]*\d[A-Za-z0-9_-]{2,})", re.I)
_TRAIL_PAREN = re.compile(r"\(((?:[^()]|\([^()]*\))*)\)\s*$")


def clean_role(raw):
    """-> dict(title, location, employer_job_id, pay_text, closes_on, clean). Only a trailing parenthetical that
    classifies completely (location / req id / pay / close date) is removed from the title; otherwise the cell
    is left whole (an explicit `req X` is still extracted, since it is unambiguous)."""
    raw = clean_text(raw)
    out = dict(title=raw, location=None, employer_job_id=None, pay_text=None, closes_on=None, clean=False)
    m = _TRAIL_PAREN.search(raw)
    if m and raw[:m.start()].strip():
        protected = re.sub(r"(\d),(\d{3})", "\\1\x00\\2", m[1])
        parts = [p.strip().replace("\x00", ",") for p in re.split(r"[;,]", protected) if p.strip()]
        locs, reqs, pays, closes, ok = [], [], [], [], bool(parts)
        place = []
        for p in parts:
            cm = _CLOSES.search(p)
            if cm:
                closes.append(cm[1])
            elif _PAY.search(p):
                pays.append(p)
            elif _PAY_NOISE.match(p):
                pass
            elif _REQ_LABELLED.search(p) or _REQ_PLATFORM.match(p) or _REQ_BARE.match(p):
                mm = _REQ_LABELLED.search(p) or _REQ_PLATFORM.match(p) or _REQ_BARE.match(p)
                reqs.append((bool(re.search(r"req", p, re.I)), mm[1] if mm.groups() else mm[0]))
            elif _LOC.search(p):
                locs.append(p)
            elif re.fullmatch(r"[A-Z][A-Za-z.\-]+(?: [A-Za-z.\-]+)?", p):
                place.append(p)                                  # a bare place name ("Irving"), kept only beside a clear location
            else:
                ok = False
        if ok and place and not locs:
            ok = False
        if ok:
            locs = [x for x in parts if x in locs or x in place]
            reqs.sort(key=lambda r: not r[0])
            out.update(title=raw[:m.start()].strip().rstrip(",;-—– ").strip(), clean=True,
                       location=", ".join(locs) or None, employer_job_id=reqs[0][1] if reqs else None,
                       pay_text=", ".join(pays) or None, closes_on=parse_date(closes[0]) if closes else None)
            return out
    rm = _REQ_ANYWHERE.search(raw)
    if rm:
        out["employer_job_id"] = rm[1]
    return out


_NAME = re.compile(r"^[A-Z][A-Za-z'’.\-]+(?: [A-Z][A-Za-z'’.\-]+){1,3}$")
_NOT_NAME = {"not", "passed", "resume", "no", "scheduled", "hiring", "human", "talent", "inbound", "screen",
             "rejected", "applied", "pending", "recruiter", "undisclosed", "ats", "hr"}
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"\(?\b\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b")


def parse_recruiters(cell):
    """Recruiter cell -> (contacts, residual). A contact dict has name/title/email/phone/linkedin. `residual` is the
    cell text when it holds more than bare names, so nothing is lost; free text with no name is all residual."""
    raw = (cell or "").strip()
    if raw in ("", "—", "-", "~") or parse_date(raw):
        return [], ("" if raw in ("", "—", "-", "~") else raw)
    text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I).replace("\\|", "|")
    contacts = []
    segments = [s for s in re.split(r"\s\|\s|\s&\s", text) if s.strip()]
    for seg in segments:
        link = re.match(r"\[([^\]]+)\]\((https?://[^)]+)\)", seg.strip())
        head = link[1] if link else re.split(r"[,(<\n]", seg.strip(), maxsplit=1)[0].strip()
        if not _NAME.match(head) or head.split()[0].lower() in _NOT_NAME or len(head) > 40:
            continue
        email = _EMAIL.search(seg)
        phone = _PHONE.search(seg)
        title = None
        tm = re.match(r"[^,]*,\s*([^,(<\n]{3,60})", seg.strip())
        if tm and re.search(r"recruit|talent|director|manager|human|HR\b|lead|scout|specialist", tm[1], re.I):
            title = tm[1].strip()
        contacts.append(dict(name=head, title=title, email=email[0] if email else None,
                             phone=phone[0] if phone else None, linkedin=link[2] if link else None))
    only_names = len(contacts) == len(segments) and all(
        len(re.sub(r"\s+", " ", s).strip()) <= 40 or re.match(r"\[", s.strip()) for s in segments)
    return contacts, ("" if (contacts and only_names) else clean_text(raw))


# ---------------------------------------------------------------- tracker parse
@dataclass
class Row:
    idx: int
    section: str
    raw: str
    date_raw: str
    company: str
    role_raw: str
    status_raw: str
    status_date_raw: str
    recruiter_raw: str
    note_cell: str
    follow_up_raw: str
    posting_cell: str
    ragged: bool = False
    extra_raws: list = field(default_factory=list)
    merged: list = field(default_factory=list)   # raw rows of same-job duplicates folded into this one
    # derived
    role: dict = field(default_factory=dict)
    date_applied: object = None
    status_date: object = None
    status: str = "built"
    confident: bool = True
    status_note: str = ""
    posting_id: object = None
    posting_how: str = "none"        # cell | exact | req | none
    candidates: list = field(default_factory=list)
    no_posting: bool = False
    employer_job_id: object = None
    app_no: int = 0
    folder: object = None
    folder_how: str = ""
    contacts: list = field(default_factory=list)
    recruiter_residual: str = ""


def parse_tracker_file(path):
    """-> (rows, networks, dropped). Header-driven: columns are found by name; a row with extra cells (a stray
    pipe) has the overflow merged into the free-text column; a short row is padded."""
    rows, networks, dropped = [], [], []
    section, header = "", None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            section, header = line.strip("# ").strip(), None
            continue
        s = line.strip()
        if not s.startswith("|"):
            header = None
            continue
        if re.match(r"^\|[\s|:-]+\|?$", s) and "-" in s:
            continue
        cells = split_cells(s)
        low = [c.lower() for c in cells]
        if header is None and len(cells) >= 2 and low[1] in ("company", "platform"):
            header = low
            continue
        if section.lower() == "consultant networks":
            if header and len(cells) >= 3 and cells[1]:
                url = re.search(r"\((https?://[^)]+)\)", cells[3]) if len(cells) > 3 else None
                networks.append(dict(platform=cells[1], date_accepted=parse_date(cells[0]), status=clean_text(cells[2]),
                                     profile_url=url[1] if url else None,
                                     notes=None if len(cells) <= 3 or url or cells[3] in ("", "—") else cells[3]))
            continue
        if not any(cells[:3]) or len(cells) < 3 or not cells[1] or cells[1].startswith("---"):
            if any(c and not set(c) <= set("-") for c in cells):
                dropped.append((section, s[:160], "no company/role"))
            continue
        h = header or ["date applied", "company", "role", "status", "status date", "recruiter",
                       "close reason", "posting id"]
        ragged = len(cells) != len(h)
        if len(cells) > len(h):                                  # stray pipes: merge into the free-text column
            free = h.index("next action") if "next action" in h else (h.index("close reason") if "close reason" in h else 6)
            extra = len(cells) - len(h)
            cells = cells[:free] + [" | ".join(cells[free:free + extra + 1])] + cells[free + extra + 1:]
        cells = cells + [""] * (len(h) - len(cells))

        def col(name, default=""):
            return cells[h.index(name)] if name in h else default
        posting = cells[-1] if h and h[-1] == "posting id" else col("posting id")
        rows.append(Row(idx=len(rows), section=section, raw=s, date_raw=col("date applied"), company=clean_text(cells[1]),
                        role_raw=cells[2], status_raw=col("status"), status_date_raw=col("status date"),
                        recruiter_raw=col("recruiter"), note_cell=col("next action") or col("close reason"),
                        follow_up_raw=col("follow-up due"), posting_cell=posting, ragged=ragged))
    return rows, networks, dropped


# ---------------------------------------------------------------- postings (DuckDB, read-only)
def load_duckdb(path, rows):
    import duckdb
    con = duckdb.connect(str(path), read_only=True)
    try:
        trk = collections.defaultdict(list)
        for company, role, date_applied, mpid, kind in con.execute(
                "SELECT company, role, date_applied, matched_posting_id, match_kind FROM tracker").fetchall():
            trk[(company, role, date_applied)].append((mpid, kind))
            trk[(company, role, None)].append((mpid, kind))
        reqs = sorted({r.employer_job_id.lower() for r in rows if r.employer_job_id})
        by_req = collections.defaultdict(list)
        if reqs:
            ph = ",".join("?" * len(reqs))
            for pid, employer, req_id, title in con.execute(
                    f"SELECT posting_id, employer, lower(req_id), title FROM postings WHERE lower(req_id) IN ({ph})",
                    reqs).fetchall():
                by_req[req_id].append((pid, employer, title))
        return trk, by_req
    finally:
        con.close()


def posting_info(path, ids):
    import duckdb
    if not ids:
        return {}
    con = duckdb.connect(str(path), read_only=True)
    try:
        ph = ",".join("?" * len(ids))
        return {pid: (emp, title) for pid, emp, title in con.execute(
            f"SELECT posting_id, employer, title FROM postings WHERE posting_id IN ({ph})", list(ids)).fetchall()}
    finally:
        con.close()


def resolve_postings(rows, duck_path):
    """Fill posting_id per the rules; a first pass derives role fields and the cell, the DuckDB pass the rest."""
    for r in rows:
        cell = r.posting_cell.strip()
        if HEX20.fullmatch(cell):
            r.posting_id, r.posting_how = cell, "cell"
        elif cell.lower() == "none":
            r.no_posting = True
        elif cell and cell not in ("—", "-", "~"):
            r.employer_job_id = r.employer_job_id or cell
        if r.posting_id is None and not r.no_posting:                  # a 20-hex token anywhere in the row
            m = re.search(r"(?<![0-9a-f])[0-9a-f]{20}(?![0-9a-f])", r.raw)
            if m and not re.search(r"https?://\S*" + m[0], r.raw):
                r.posting_id, r.posting_how = m[0], "cell"
    if not duck_path:
        return
    trk, by_req = load_duckdb(duck_path, rows)
    for r in rows:
        if r.posting_id or r.no_posting:
            continue
        hits = trk.get((r.company, r.role_raw, r.date_raw)) or trk.get((r.company, r.role_raw, None)) or []
        for mpid, kind in hits:
            if kind in EXACT_KINDS and mpid:
                r.posting_id, r.posting_how = mpid, "exact"
                break
        if r.posting_id is None and r.employer_job_id:
            keys = company_keys(r.company)
            found = {pid for pid, emp, _t in by_req.get(r.employer_job_id.lower(), [])
                     if company_matches(norm_company(emp), keys)}
            if len(found) == 1:
                r.posting_id, r.posting_how = found.pop(), "req"
        if r.posting_id is None:
            r.candidates = sorted({m for m, k in hits if k == "fuzzy" and m})
    info = posting_info(duck_path, {c for r in rows for c in r.candidates})
    for r in rows:
        r.candidates = [(c, *(info.get(c) or ("?", "?"))) for c in r.candidates]


# ---------------------------------------------------------------- folders
_STOP = {"and", "of", "the", "for", "a", "in", "to", "at", "jr", "us"}
_ALIAS = {"sr": "sen", "mgr": "man", "biz": "bus", "ops": "ope", "opex": "ope", "pgm": "pro", "dir": "dir",
          "vp": "vp", "pm": "pm", "bi": "bi", "ai": "ai", "it": "it"}


def _stem(w):
    return _ALIAS.get(w, w[:3])


def _tokens(text):
    words = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", text)
    return {_stem(w.lower()) for w in words if w.lower() not in _STOP and not w.isdigit()}


def _squash(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _company_ok(company, prefix):
    p = _squash(prefix)
    if len(p) < 3:
        return False
    for key in company_keys(company) or [norm_company(company)]:
        c = _squash(key)
        fw = _squash(key.split()[0]) if key.split() else ""
        if c.startswith(p) or p.startswith(c) or (len(fw) >= 3 and p.startswith(fw)):
            return True
    return False


def match_folders(rows, apps_dir):
    """Assign r.folder / r.folder_how; returns a list of (row, problem, detail) for the report."""
    problems = []
    names = sorted(p.name for p in apps_dir.iterdir() if p.is_dir()) if apps_dir.is_dir() else []
    meta = {n: dict(prefix=n.split("_")[0], tail=_tokens("_".join(n.split("_")[1:])), low=n.lower()) for n in names}
    texts = {}

    def index_text(n):
        if n not in texts:
            p = apps_dir / n / "index.md"
            texts[n] = p.read_text(encoding="utf-8", errors="replace").lower() if p.exists() else ""
        return texts[n]

    def has_req(hay, req):
        return bool(req) and len(req) >= 4 and re.search(rf"(?<![a-z0-9]){re.escape(req.lower())}(?![a-z0-9])", hay)

    cand = {}                                                    # row idx -> (rank, [folders])
    for r in rows:
        explicit = [n for n in re.findall(r"Folder:\s*([A-Za-z0-9_.\-]+)", " ".join([r.raw, *r.extra_raws])) if n in meta]
        if explicit:
            cand[r.idx] = (0, explicit[:1])
            continue
        co = [n for n in names if _company_ok(r.company, meta[n]["prefix"])]
        req = r.employer_job_id
        by_req = [n for n in co if has_req(meta[n]["low"], req)]
        if by_req:
            cand[r.idx] = (1, by_req)
            continue
        want = _tokens(r.role["title"])
        scored = []
        for n in co:
            f = meta[n]["tail"]
            if not f:
                continue
            hit = len(f & want)
            if hit >= 2 or (len(f) == 1 and hit == 1):
                score = hit / len(f)
                if score >= 0.6:
                    scored.append((score, hit, n))
        if scored:
            best = max(s[:2] for s in scored)
            cand[r.idx] = (2, [n for s in scored if s[:2] == best for n in [s[2]]])
            continue
        if req:
            by_idx = [n for n in co if has_req(index_text(n), req)]
            if by_idx:
                cand[r.idx] = (3, by_idx)

    claims, byi = {}, {r.idx: r for r in rows}
    order = sorted(cand, key=lambda i: (cand[i][0], -(byi[i].date_applied or rows[i].status_date or dt.date.min).toordinal(), i))
    for i in order:
        r, (rank, fl) = byi[i], cand[i]
        if len(fl) != 1:
            problems.append((r, "ambiguous", ", ".join(fl[:6])))
            continue
        f = fl[0]
        if f in claims:
            problems.append((r, "collision", f"{f} already claimed by app {claims[f].app_no} ({claims[f].company})"))
            continue
        claims[f] = r
        r.folder = f"{apps_dir.name}/{f}"
        r.folder_how = ["explicit", "req", "tokens", "index"][rank]
    for r in rows:
        if r.folder is None and r.idx not in cand:
            problems.append((r, "missing", ""))
    return problems


# ---------------------------------------------------------------- derive + dedupe
def derive(rows):
    for r in rows:
        r.role = clean_role(r.role_raw)
        r.employer_job_id = r.role["employer_job_id"]
        r.date_applied = parse_date(r.date_raw)
        r.status_date = parse_date(r.status_date_raw)
        r.status, r.confident, r.status_note = map_status(r.status_raw, r.section, r.date_applied is not None)
        r.contacts, r.recruiter_residual = parse_recruiters(r.recruiter_raw)


_PROGRESS = {"built": 1, "closed-unsent": 1, "passed": 1, "applied": 2, "assumed-rejected": 3, "screen": 3,
             "rejected": 4, "interview": 4, "declined": 4, "withdrawn": 4, "offer": 5, "accepted": 6}


def merge_duplicates(rows):
    """Fold rows that are the SAME JOB into one: same normalized company and cleaned role, and the same employer
    req id, or (neither has one) a date applied within 3 days of each other. Different req ids never merge.
    Keeps the row with the latest status date (ties: most advanced status). -> (survivors, [(kept, [merged], why)])."""
    groups = collections.defaultdict(list)
    for r in rows:
        groups[(norm_company(r.company), re.sub(r"\s+", " ", r.role["title"].lower()).strip())].append(r)
    parent = {r.idx: r.idx for r in rows}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for g in groups.values():
        for i, a in enumerate(g):
            for b in g[i + 1:]:
                ra, rb = (a.employer_job_id or "").lower(), (b.employer_job_id or "").lower()
                if ra or rb:
                    same = ra == rb
                else:
                    da, db_ = a.date_applied, b.date_applied
                    same = (da is None and db_ is None) or (da is not None and db_ is not None and abs((da - db_).days) <= 3)
                if same:
                    parent[find(b.idx)] = find(a.idx)
    clusters = collections.defaultdict(list)
    for r in rows:
        clusters[find(r.idx)].append(r)
    survivors, merges = [], []
    for cl in clusters.values():
        if len(cl) == 1:
            survivors.append(cl[0])
            continue
        cl.sort(key=lambda r: (r.status_date or dt.date.min, _PROGRESS.get(r.status, 0), -r.idx), reverse=True)
        keep, others = cl[0], cl[1:]
        for o in others:
            keep.merged.append(f"[{o.section}] " + re.sub(r"\s{2,}", " ", o.raw))
            if keep.posting_id is None and o.posting_id:
                keep.posting_id, keep.posting_how = o.posting_id, o.posting_how
            keep.employer_job_id = keep.employer_job_id or o.employer_job_id
            keep.date_applied = keep.date_applied or o.date_applied
            keep.note_cell = keep.note_cell or o.note_cell
            keep.follow_up_raw = keep.follow_up_raw or o.follow_up_raw
            have = {c["name"].lower() for c in keep.contacts}
            keep.contacts += [c for c in o.contacts if c["name"].lower() not in have]
            if o.recruiter_residual and o.recruiter_residual not in keep.recruiter_residual:
                keep.recruiter_residual = "\n".join(x for x in (keep.recruiter_residual, o.recruiter_residual) if x)
            keep.extra_raws.append(o.raw)
        why = ("same req id " + keep.employer_job_id) if keep.employer_job_id else "no req id; date applied within 3 days"
        merges.append((keep, others, why))
        survivors.append(keep)
    survivors.sort(key=lambda r: r.idx)
    return survivors, merges


def dedupe_postings(rows):
    """posting_id is unique: keep it on the most recent row, return [(row, kept_row)] for the others."""
    by = collections.defaultdict(list)
    for r in rows:
        if r.posting_id:
            by[r.posting_id].append(r)
    lost = []
    recency = lambda r: (r.date_applied or dt.date.min, r.status_date or dt.date.min, -r.idx)  # noqa: E731
    for pid, rs in by.items():
        if len(rs) > 1:
            rs.sort(key=recency, reverse=True)
            for loser in rs[1:]:
                lost.append((loser, rs[0]))
                loser.posting_id, loser.posting_how = None, "none"
    return lost


def insertion_order(rows):
    far = dt.date.max
    return sorted(rows, key=lambda r: (r.date_applied is None, r.date_applied or far, r.status_date or far, r.idx))


# ---------------------------------------------------------------- database
def load_db(conn, ordered, networks, force):
    bad = conn.execute("SELECT count(*) AS n FROM tracker.event WHERE actor <> %s", [ACTOR]).fetchone()["n"]
    if bad and not force:
        raise SystemExit(f"refusing: {bad} event(s) have an actor other than '{ACTOR}' (real edits would be lost); "
                         "use --force to wipe anyway")
    contact_ids, today = {}, dt.date.today()
    with conn.transaction():
        conn.execute("TRUNCATE tracker.application, tracker.event, tracker.contact, tracker.app_contact, "
                     "tracker.network RESTART IDENTITY CASCADE")
        for r in ordered:
            notes = []
            if r.recruiter_residual:
                notes.append(f"Recruiter: {r.recruiter_residual}")
            closed = r.section.lower() == "closed"
            sdate = r.status_date or r.date_applied or today
            row = conn.execute(
                """INSERT INTO tracker.application
                     (posting_id, employer_job_id, company, role, location, pay_text, source, folder, status,
                      status_date, date_applied, closes_on, next_action, follow_up_due, close_reason, notes)
                   VALUES (%s,%s,%s,%s,%s,%s,'migrated',%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING app_id""",
                [r.posting_id, r.employer_job_id, r.company, r.role["title"], r.role["location"], r.role["pay_text"],
                 r.folder, r.status, sdate, r.date_applied, r.role["closes_on"],
                 None if closed else (clean_text(r.note_cell) or None), parse_date(r.follow_up_raw),
                 (clean_text(r.note_cell) or None) if closed else None, "\n".join(notes) or None]).fetchone()
            app_id = row["app_id"]
            conn.execute("INSERT INTO tracker.event (app_id, kind, actor, note) VALUES (%s,'migrated',%s,%s)",
                         [app_id, ACTOR, "[%s] %s" % (r.section, re.sub(r"\s{2,}", " ", r.raw))])
            for m in r.merged:
                conn.execute("INSERT INTO tracker.event (app_id, kind, actor, note) VALUES (%s,'migrated',%s,%s)",
                             [app_id, ACTOR, "[merged duplicate] " + m])
            conn.execute("INSERT INTO tracker.event (app_id, kind, status, status_date, new_value, actor) "
                         "VALUES (%s,'status',%s,%s,%s,%s)", [app_id, r.status, sdate, r.status, ACTOR])
            for c in r.contacts:
                key = (c["name"].lower(), r.company.lower())
                if key not in contact_ids:
                    contact_ids[key] = conn.execute(
                        "INSERT INTO tracker.contact (name, title, company, email, phone, linkedin) "
                        "VALUES (%s,%s,%s,%s,%s,%s) RETURNING contact_id",
                        [c["name"], c["title"], r.company, c["email"], c["phone"], c["linkedin"]]).fetchone()["contact_id"]
                conn.execute("INSERT INTO tracker.app_contact (app_id, contact_id, role) VALUES (%s,%s,'recruiter') "
                             "ON CONFLICT DO NOTHING", [app_id, contact_ids[key]])
        for n in networks:
            conn.execute("INSERT INTO tracker.network (platform, date_accepted, status, profile_url, notes) "
                         "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (platform) DO NOTHING",
                         [n["platform"], n["date_accepted"], n["status"], n["profile_url"], n["notes"]])
    return len(contact_ids)


# ---------------------------------------------------------------- report
def _cell(s, n=110):
    s = clean_text(str(s)).replace("|", "/")
    return s if len(s) <= n else s[:n - 1] + "…"


def table(head, body):
    if not body:
        return "_None._\n"
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in body]
    return "\n".join(out) + "\n"


def write_report(path, rows, networks, dropped, lost, folder_problems, dry_run, ordered, contacts_n, merges=(), parsed=None):
    by_sec = collections.Counter(r.section for r in rows)
    by_status = collections.Counter(r.status for r in rows)
    how = collections.Counter(r.posting_how for r in rows)
    with_cand = sum(1 for r in rows if r.posting_id is None and r.candidates)
    prob = collections.Counter(p for _r, p, _d in folder_problems)
    matched = sum(1 for r in rows if r.folder)
    rid = lambda r: f"{r.date_raw or '—'} / {r.company} / {r.role['title']}"  # noqa: E731
    L = ["---", "node_id: SYS:tracker-migration-review-20261021", "node_type: resource",
         "tags: [#job-search #tracker]", "ai_edit_policy: free-edit", "---", "",
         "# Tracker migration review", "",
         f"AI_USAGE: Generated by scripts/tracker_migrate.py on {dt.date.today()} ({'dry run, no database touched' if dry_run else 'loaded'}); review the tables below and tell Claude which rows to fix before the real cutover.",
         "", "## Counts", "",
         f"{parsed or len(rows)} rows parsed, {sum(len(m[1]) for m in merges)} merged away as same-job duplicates, so {len(rows)} applications; {len(networks)} network rows, {len(dropped)} dropped, {contacts_n} contacts created.", "",
         table(["Section", "Rows"], sorted(by_section_items(by_sec))), "",
         table(["Mapped status", "Rows"], by_status.most_common()), "",
         table(["Posting id", "Rows"], [("filled from the Posting ID cell", how["cell"]),
                                        ("filled, DuckDB exact match", how["exact"]),
                                        ("filled, unique employer req id in postings", how["req"]),
                                        ("not filled, fuzzy candidate(s) listed below", with_cand),
                                        ("not filled, none", len(rows) - how["cell"] - how["exact"] - how["req"] - with_cand)]), "",
         table(["Folder", "Rows"], [("matched", matched), ("missing", prob["missing"]),
                                    ("ambiguous", prob["ambiguous"]), ("collision (second claimant)", prob["collision"])]), "",
         f"Row-matched folder methods: {dict(collections.Counter(r.folder_how for r in rows if r.folder))}", "",
         "## Status: unmapped or low confidence (raw text to chosen status)", "",
         "Grouped by raw status text and section. Confident rules are not listed.", ""]
    grp = collections.defaultdict(list)
    for r in rows:
        if not r.confident:
            grp[(clean_text(r.status_raw), r.section, r.status, r.status_note)].append(r)
    L.append(table(["Raw status", "Section", "Chosen", "Why", "Rows", "Example"],
                   [(k[0], k[1], k[2], k[3], len(v), rid(v[0])) for k, v in sorted(grp.items(), key=lambda kv: -len(kv[1]))]))
    L += ["", "## Posting id: fuzzy candidates (never filled)", "",
          table(["Row", "Candidate posting id", "Employer", "Title"],
                [(rid(r), c[0], c[1], c[2]) for r in rows if r.posting_id is None for c in r.candidates]), "",
          "## Folder problems", "",
          table(["Row", "Problem", "Detail"], [(rid(r), p, d) for r, p, d in folder_problems if p != "missing"]), "",
          f"### Missing folders ({prob['missing']})", "",
          table(["Row", "Section", "Status"], [(rid(r), r.section, r.status) for r, p, _d in folder_problems if p == "missing"]), "",
          "## Merged duplicates", "",
          "Same company, same cleaned role, and the same req id (or no req id on either and date applied within 3 days). Different req ids are never merged. Each merged raw row is kept as a `migrated` event on the kept application.", "",
          table(["Kept row", "Merged rows", "Why"], [(rid(k) + f" [{k.status}]", " ;; ".join(f"{o.date_raw or '—'} {o.status_raw}" for o in oth), why) for k, oth, why in merges]), "",
          "## Dropped and duplicate rows", "",
          table(["Row", "Why"], [(f"[{s}] {t}", why) for s, t, why in dropped]
                + [(rid(l), f"posting_id also on app {k.app_no} ({rid(k)}); kept on the more recent row") for l, k in lost]), "",
          "### Same company, role and date on more than one row (kept; for review)", ""]
    seen = collections.defaultdict(list)
    for r in rows:
        seen[(r.company, r.role["title"], r.date_raw)].append(r)
    L.append(table(["Row", "Count"], [(f"{k[2]} / {k[0]} / {k[1]}", len(v)) for k, v in seen.items() if len(v) > 1]))
    ragged = [r for r in rows if r.ragged]
    L += ["", f"## Ragged rows (cell count differs from the header): {len(ragged)}", "",
          table(["Row"], [(rid(r),) for r in ragged]), "",
          "## Other", "",
          f"Rows with no status date (fell back to date applied, else today): {sum(1 for r in rows if r.status_date is None)}.", "",
          f"Roles cleaned into location / req / pay / closes: {sum(1 for r in rows if r.role['clean'])} of {len(rows)}.", ""]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(L), encoding="utf-8")


def by_section_items(c):
    return list(c.items())


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tracker", required=True)
    ap.add_argument("--duckdb", required=True)
    ap.add_argument("--db", default=None, help="PostgreSQL database (default: $TRACKER_PGDATABASE or resume)")
    ap.add_argument("--report", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    tracker = Path(a.tracker)
    rows, networks, dropped = parse_tracker_file(tracker)
    derive(rows)
    resolve_postings(rows, a.duckdb)
    parsed = len(rows)
    rows, merges = merge_duplicates(rows)
    lost = dedupe_postings(rows)
    ordered = insertion_order(rows)
    for n, r in enumerate(ordered, 1):
        r.app_no = n
    folder_problems = match_folders(rows, tracker.parent / "Applications")
    contacts_n = 0
    if not a.dry_run:
        from backend.tracker import db
        with db.connect(a.db) as conn:
            contacts_n = load_db(conn, ordered, networks, a.force)
    else:
        contacts_n = len({(c["name"].lower(), r.company.lower()) for r in rows for c in r.contacts})
    write_report(a.report, rows, networks, dropped, lost, folder_problems, a.dry_run, ordered, contacts_n, merges, parsed)
    print(f"{'dry run: ' if a.dry_run else ''}{len(rows)} applications, {len(networks)} networks, {contacts_n} contacts; report {a.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
