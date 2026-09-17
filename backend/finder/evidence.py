"""The user's background as evidence units (sprint plan §15.1, amended by §16.5).

A TOML manifest (`evidence.local.toml` at the repo root, gitignored; `JOBSEARCH_EVIDENCE` overrides) lists
sources. Each source becomes 40-600 character units with a kind (achievement 1.0 · duty 0.9 · narrative 0.8 ·
method 0.7) that later scales coverage credit. `[[guard]]` files are loaded for Phase 4 prompts only and are never
embedded. `not_in_record` terms force a gap; `light_in_record` terms cap a requirement at partial.

A `postgres` source reads rows straight from a database through `psql --csv`: `path` is a libpq connection string,
normally `$VAR` naming a `.env` entry so the password stays out of the manifest, and `query` is the SELECT. Its rows are handled exactly like a csv
source's. `evidence_units` is a cache of the sources, not a record: `ensure_current` re-syncs it before coverage.

The unit text never leaves the machine: it lives in `evidence_units` inside the local DuckDB file.
"""
import csv
import glob
import hashlib
import io
import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .labels import normalize_text

REPO = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = REPO / "evidence.local.toml"
KIND_WEIGHTS = {"achievement": 1.0, "duty": 0.9, "narrative": 0.8, "method": 0.7}
SOURCE_TYPES = ("csv", "markdown", "pdf", "html", "text", "postgres")
MIN_UNIT, MAX_UNIT = 40, 600


@dataclass
class Source:
    name: str
    type: str
    path: str
    kind: str = "narrative"
    include: list = field(default_factory=list)
    exclude: list = field(default_factory=list)
    text_columns: list = field(default_factory=list)
    ref_columns: list = field(default_factory=list)
    skip_headings: list = field(default_factory=list)   # blocks under a heading containing one of these are dropped
    skip_patterns: list = field(default_factory=list)   # blocks whose text matches one of these regexes are dropped
    query: str = ""                                      # postgres only: the SELECT whose rows become blocks


@dataclass
class Manifest:
    path: str
    embed_model: str
    sources: list
    guards: list
    not_in_record: list
    light_in_record: list


@dataclass
class EvidenceUnit:
    unit_id: str
    source: str
    kind: str
    ref: str
    text: str
    weight: float


def manifest_path(path: Optional[str] = None) -> Path:
    return Path(os.path.expanduser(path or os.getenv("JOBSEARCH_EVIDENCE") or str(DEFAULT_MANIFEST)))


def load_manifest(path: Optional[str] = None) -> Manifest:
    """Parses the manifest. Relative source paths resolve against the manifest's folder; `~` expands."""
    from .embed import EMBED_MODEL
    mp = manifest_path(path)
    data = tomllib.loads(mp.read_text(encoding="utf-8"))
    base = mp.parent

    def resolve(p: str, type_: str = "") -> str:
        if type_ == "postgres":                            # a connection string, not a file; `$VAR` expands at read time
            return p
        p = os.path.expanduser(p)
        return p if os.path.isabs(p) else str((base / p).resolve())

    sources = []
    for raw in data.get("source", []):
        kind = raw.get("kind", "narrative")
        if kind not in KIND_WEIGHTS:
            raise ValueError(f"source {raw.get('name')!r}: kind {kind!r} is not one of {sorted(KIND_WEIGHTS)}")
        if raw.get("type") not in SOURCE_TYPES:
            raise ValueError(f"source {raw.get('name')!r}: type {raw.get('type')!r} is not one of {SOURCE_TYPES}")
        _require_query(raw)
        sources.append(Source(raw["name"], raw["type"], resolve(raw["path"], raw["type"]), kind,
                              list(raw.get("include", [])), list(raw.get("exclude", [])),
                              list(raw.get("text_columns", [])), list(raw.get("ref_columns", [])),
                              list(raw.get("skip_headings", [])), list(raw.get("skip_patterns", [])),
                              raw.get("query", "")))
    guards = []
    for g in data.get("guard", []):
        _require_query(g)
        gtype = g.get("type", "csv")
        guards.append(Source(g["name"], gtype, resolve(g["path"], gtype), "guard", [], [],
                             list(g.get("text_columns", [])), list(g.get("ref_columns", [])), query=g.get("query", "")))
    return Manifest(str(mp), data.get("embed_model", EMBED_MODEL), sources, guards,
                    list(data.get("not_in_record", [])), list(data.get("light_in_record", [])))


def _require_query(raw: dict) -> None:
    if raw.get("type") == "postgres" and not raw.get("query"):
        raise ValueError(f"source {raw.get('name')!r}: a postgres source needs a `query`")


# ---------------------------------------------------------------- text -> units
def _split_long(text: str) -> list:
    """Pieces of at most MAX_UNIT characters: sentence ends, then ';' / ' / ', then word boundaries."""
    if len(text) <= MAX_UNIT:
        return [text]
    out, cur = [], ""
    for sent in re.split(r"(?<=[.!?])\s+|;\s+|\s+/\s*|(?<=\w)/(?=\[)", text):
        sent = sent.strip()
        while len(sent) > MAX_UNIT:
            cut = sent.rfind(" ", 0, MAX_UNIT)
            cut = cut if cut > MIN_UNIT else MAX_UNIT
            out.append(sent[:cut].strip())
            sent = sent[cut:].strip()
        if cur and len(cur) + 1 + len(sent) > MAX_UNIT:
            out.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}".strip()
    if cur:
        out.append(cur)
    return out


def _clean(text: str) -> str:
    text = normalize_text(text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)         # markdown links -> their text
    text = re.sub(r"[*_`>]+", " ", text)                           # emphasis, code, quote markers
    return re.sub(r"\s+", " ", text).strip()


def _units_from_blocks(blocks: list) -> list:
    """(ref, text) blocks -> (ref, unit text): long blocks split, short ones merged with the next in the same ref."""
    out, pending = [], None
    for ref, text in blocks:
        text = _clean(text)
        if not text:
            continue
        if pending and pending[0] == ref:
            text = f"{pending[1]} {text}"
        elif pending and len(pending[1]) >= 15:
            out.append(pending)
        pending = None
        for piece in _split_long(text):
            if len(piece) < MIN_UNIT:
                pending = (ref, piece)
            else:
                out.append((ref, piece))
    if pending and len(pending[1]) >= 15:
        out.append(pending)
    return out


def markdown_blocks(text: str, doc: str) -> list:
    """(ref, text) per paragraph / list item / table row; ref = document name › nearest heading."""
    text = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", text, flags=re.S)      # frontmatter
    text = re.sub(r"^```.*?^```\s*$", "", text, flags=re.S | re.M)       # code fences
    blocks, heading, para = [], "", []

    def flush():
        if para:
            blocks.append((f"{doc} › {heading}" if heading else doc, " ".join(para)))
            para.clear()

    for line in text.splitlines():
        s = line.strip()
        h = re.match(r"^#{1,6}\s+(.*)", s)
        if h:
            flush()
            heading = _clean(h.group(1))
        elif not s or re.fullmatch(r"[-*_]{3,}", s):
            flush()
        elif s.startswith("|"):
            flush()
            if not set(s) <= set("|-: "):
                blocks.append((f"{doc} › {heading}" if heading else doc,
                               " · ".join(c.strip() for c in s.strip("|").split("|") if c.strip())))
        elif re.match(r"^([-*+]|\d+[.)])\s+", s):
            flush()
            para.append(re.sub(r"^([-*+]|\d+[.)])\s+", "", s))
            flush()
        else:
            para.append(s)
    flush()
    return blocks


def html_blocks(text: str, doc: str) -> list:
    """Visible text per block element (nav, header, footer, script, style dropped); ref = nearest heading."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(text, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg", "form", "button"]):
        tag.decompose()
    blocks, heading = [], ""
    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "blockquote", "td", "dd", "figcaption"]):
        if el.name in ("h1", "h2", "h3", "h4"):
            heading = _clean(el.get_text(" "))
            continue
        if el.find(["p", "li", "blockquote", "td"]):
            continue
        blocks.append((f"{doc} › {heading}" if heading else doc, el.get_text(" ")))
    return blocks


def pdf_text(path: str) -> str:
    """pdftotext in reading order (not -layout: two-column exports interleave), else pypdf."""
    if shutil.which("pdftotext"):
        return subprocess.run(["pdftotext", path, "-"], capture_output=True, text=True, check=True).stdout
    from pypdf import PdfReader
    return "\n\n".join(page.extract_text() or "" for page in PdfReader(path).pages)


def plain_blocks(text: str, doc: str) -> list:
    """Paragraphs split on blank lines; single line breaks inside a paragraph are joined."""
    return [(doc, " ".join(p.split())) for p in re.split(r"\n\s*\n", text) if p.strip()]


def _files(src: Source) -> list:
    if os.path.isfile(src.path):
        return [src.path]
    patterns = src.include or {"markdown": ["**/*.md"], "html": ["*.html"], "text": ["**/*.txt"],
                               "pdf": ["**/*.pdf"], "csv": ["**/*.csv"]}[src.type]
    found = {f for pat in patterns for f in glob.glob(os.path.join(src.path, pat), recursive=True)}
    excluded = {f for pat in src.exclude for f in glob.glob(os.path.join(src.path, pat), recursive=True)}
    excluded |= {f for f in found if os.path.basename(f) in src.exclude}
    return sorted(found - excluded)


def pg_rows(src: Source) -> list:
    """Rows of `src.query` via `psql --csv`. Raises on any failure: an unreachable database must never read as an
    empty source, or a rebuild would delete every unit that source produced."""
    if not shutil.which("psql"):
        raise RuntimeError(f"source {src.name!r}: psql is not installed")
    conn = os.path.expandvars(src.path)
    if "$" in conn:
        raise RuntimeError(f"source {src.name!r}: {src.path} is not set (add it to .env)")
    run = subprocess.run(["psql", conn, "--csv", "--no-psqlrc", "-v", "ON_ERROR_STOP=1", "-c", src.query],
                         capture_output=True, text=True, timeout=60)
    if run.returncode != 0:
        raise RuntimeError(f"source {src.name!r}: psql failed: {run.stderr.strip()[:300]}")
    return list(csv.DictReader(io.StringIO(run.stdout)))


def csv_rows(src: Source) -> list:
    if src.type == "postgres":
        return pg_rows(src)
    rows = []
    for path in _files(src):
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows.extend(csv.DictReader(fh))
    return rows


def source_blocks(src: Source) -> list:
    """(ref, text) blocks for one source, minus the source's skip_headings / skip_patterns."""
    blocks = _raw_blocks(src)
    heads = [h.lower() for h in src.skip_headings]
    pats = [re.compile(p) for p in src.skip_patterns]
    return [(ref, text) for ref, text in blocks
            if not any(h in ref.split(" › ", 1)[-1].lower() for h in heads if " › " in ref)
            and not any(rx.search(_clean(text)) for rx in pats)]


def _raw_blocks(src: Source) -> list:
    if src.type in ("csv", "postgres"):
        blocks = []
        for i, row in enumerate(csv_rows(src)):
            text = " ".join((row.get(c) or "").strip() for c in src.text_columns if (row.get(c) or "").strip())
            ref = " · ".join((row.get(c) or "").strip() for c in src.ref_columns if (row.get(c) or "").strip())
            blocks.append((ref or f"{src.name} row {i + 1}", text))
        return blocks
    blocks = []
    for path in _files(src):
        doc = Path(path).stem
        if src.type == "pdf":
            blocks.extend(plain_blocks(pdf_text(path), doc))
            continue
        raw = Path(path).read_text(encoding="utf-8", errors="ignore")
        blocks.extend({"markdown": markdown_blocks, "html": html_blocks, "text": plain_blocks}[src.type](raw, doc))
    return blocks


def _unit_id(source: str, ref: str, text: str) -> str:
    return hashlib.sha1(f"{source}|{ref}|{text}".encode("utf-8")).hexdigest()[:20]


def iter_units(manifest: Manifest) -> list:
    """Every evidence unit; an exact duplicate text across sources is kept once, at the highest weight."""
    best = {}
    for src in manifest.sources:
        for ref, text in _units_from_blocks(source_blocks(src)):
            unit = EvidenceUnit(_unit_id(src.name, ref, text), src.name, src.kind, ref[:200], text,
                                KIND_WEIGHTS[src.kind])
            key = text.lower()
            if key not in best or unit.weight > best[key].weight:
                best[key] = unit
    return list(best.values())


def guard_texts(manifest: Manifest) -> list:
    """Claim-guard strings for Phase 4 prompts (never embedded)."""
    out = []
    for g in manifest.guards:
        for row in csv_rows(g):
            text = " ".join((row.get(c) or "").strip() for c in g.text_columns if (row.get(c) or "").strip())
            if text:
                out.append(text)
    return out


def evidence_version(units: list, embed_model: str) -> str:
    payload = "|".join(sorted(u.unit_id for u in units)) + "|" + embed_model
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- store
def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def check(manifest: Manifest, log=print) -> bool:
    """Every source path found, unit counts, three sample units each; False when a path is missing."""
    ok = True
    for src in [*manifest.sources, *manifest.guards]:
        if src.type == "postgres":
            try:
                rows = pg_rows(src)
            except RuntimeError as exc:
                log(f"MISSING  {src.name:<20} postgres {exc}")
                ok = False
                continue
            if src.kind == "guard":
                log(f"guard    {src.name:<20} {len(rows):>5} rows (Phase 4 prompts only; never embedded)")
                continue
            units = _units_from_blocks(source_blocks(src))
            log(f"ok       {src.name:<20} postgres kind {src.kind:<11} rows {len(rows):>4} · units {len(units):>5} "
                f"· chars {sum(len(t) for _, t in units):>7}")
            for ref, text in units[:3]:
                log(f"           [{ref[:50]}] {text[:110]}")
            continue
        exists = os.path.exists(src.path)
        files = _files(src) if exists else []
        if not exists or not files:
            log(f"MISSING  {src.name:<20} {src.type:<8} {src.path}")
            ok = False
            continue
        if src.kind == "guard":
            log(f"guard    {src.name:<20} {len(csv_rows(src)):>5} rows (Phase 4 prompts only; never embedded)")
            continue
        units = _units_from_blocks(source_blocks(src))
        chars = sum(len(t) for _, t in units)
        log(f"ok       {src.name:<20} {src.type:<8} kind {src.kind:<11} files {len(files):>3} · units {len(units):>5} "
            f"· chars {chars:>7}")
        for ref, text in units[:3]:
            log(f"           [{ref[:50]}] {text[:110]}")
    log(f"not_in_record: {', '.join(manifest.not_in_record) or '(none)'}")
    log(f"light_in_record: {', '.join(manifest.light_in_record) or '(none)'}")
    return ok


def rebuild(con, manifest: Manifest, encoder, log=print, units: Optional[list] = None) -> dict:
    """Syncs `evidence_units` to the manifest: new units embedded, units no longer produced deleted. Refuses when a
    listed source that has stored units now produces none (a moved file or a down database, not a real deletion)."""
    from .embed import vectors_json
    units = iter_units(manifest) if units is None else units
    produced = {u.source for u in units}
    stored = {r[0] for r in con.execute("SELECT DISTINCT source FROM evidence_units WHERE model = ?",
                                        [manifest.embed_model]).fetchall()}
    emptied = sorted(s.name for s in manifest.sources if s.name in stored and s.name not in produced)
    if emptied:
        raise RuntimeError(f"evidence rebuild refused: {', '.join(emptied)} produced no units but has stored ones "
                           "(missing file or unreachable database?); fix the source or remove it from the manifest")
    have = {r[0] for r in con.execute("SELECT unit_id FROM evidence_units WHERE model = ?",
                                      [manifest.embed_model]).fetchall()}
    new = [u for u in units if u.unit_id not in have]
    keep = sorted(u.unit_id for u in units)
    con.execute("BEGIN")
    try:
        con.execute("DELETE FROM evidence_units WHERE model != ? OR unit_id NOT IN (SELECT unnest(?::VARCHAR[]))",
                    [manifest.embed_model, keep])
        if new:
            vecs = encoder.encode([u.text for u in new], query=False)
            _insert_units(con, new, vecs, manifest.embed_model, vectors_json)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    version = evidence_version(units, manifest.embed_model)
    stats = {"units": len(units), "embedded": len(new), "evidence_version": version}
    log(f"Evidence: {len(units)} units, {len(new)} newly embedded, version {version}")
    return stats


def _insert_units(con, units: list, vecs, model: str, vectors_json) -> None:
    import json
    rows = json.loads(vectors_json(vecs))
    payload = json.dumps([{"unit_id": u.unit_id, "source": u.source, "kind": u.kind, "ref": u.ref, "text": u.text,
                           "weight": u.weight, "vec": v} for u, v in zip(units, rows)])
    shape = json.dumps([{"unit_id": "VARCHAR", "source": "VARCHAR", "kind": "VARCHAR", "ref": "VARCHAR",
                         "text": "VARCHAR", "weight": "DOUBLE", "vec": "FLOAT[]"}])
    con.execute(f"""
        INSERT OR REPLACE INTO evidence_units (unit_id, source, kind, ref, text, weight, model, vector, content_hash,
                                               embedded_at)
        SELECT unit_id, source, kind, ref, text, weight, $3, vec::FLOAT[{len(rows[0]) if rows else 384}], md5(text), $4
        FROM (SELECT unnest(json_transform($1, $2), recursive := true))""", [payload, shape, model, _now()])


def load_matrix(con, model: str) -> tuple:
    """(units as dicts, float32 matrix) of the stored evidence for `model`, in unit_id order."""
    from .embed import stack
    cur = con.execute("SELECT unit_id, source, kind, ref, text, weight, vector FROM evidence_units WHERE model = ? "
                      "ORDER BY unit_id", [model]).fetchnumpy()
    units = [{"unit_id": str(cur["unit_id"][i]), "source": str(cur["source"][i]), "kind": str(cur["kind"][i]),
              "ref": str(cur["ref"][i]), "text": str(cur["text"][i]), "weight": float(cur["weight"][i])}
             for i in range(len(cur["unit_id"]))]
    return units, stack(cur["vector"])


def ensure_current(con, manifest: Manifest, encoder, log=print) -> Optional[str]:
    """Re-syncs `evidence_units` when the sources changed since the last build (new bullets, edited text). Reading
    the sources is cheap; only new units are embedded. When a source cannot be read, the stored evidence is used
    as-is with a warning, so a down database never blocks coverage. Returns the evidence version in effect."""
    model = manifest.embed_model
    stored = stored_version(con, model)
    try:
        units = iter_units(manifest)
    except (OSError, FileNotFoundError, subprocess.CalledProcessError, tomllib.TOMLDecodeError) as exc:
        log(f"Evidence: sources unreadable ({type(exc).__name__}: {exc}); using stored version {stored}")
        return stored
    if evidence_version(units, model) == stored:
        return stored
    try:
        return rebuild(con, manifest, encoder, log=log, units=units)["evidence_version"]
    except RuntimeError as exc:
        log(f"Evidence: {exc}; using stored version {stored}")
        return stored


def stored_version(con, model: str) -> Optional[str]:
    ids = [r[0] for r in con.execute("SELECT unit_id FROM evidence_units WHERE model = ?", [model]).fetchall()]
    if not ids:
        return None
    return hashlib.sha1(("|".join(sorted(ids)) + "|" + model).encode("utf-8")).hexdigest()[:12]
