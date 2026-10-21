"""Application folder naming and creation for `trk add` (design section 5).

The folder path stored in `tracker.application.folder` is RELATIVE TO THE Job_Search DIRECTORY
(for example `Applications/AcmeCorp_SrAnalyst_R123`). Folders are never renamed or overwritten.
"""
import re
import unicodedata
from pathlib import Path

from backend.tracker.store import TrackerError

APPLICATIONS_DIR = "Applications"
COMPANY_MAX = 30
ROLE_MAX = 40


def camel(text, maxlen):
    """CamelCase alphanumerics only, cut at a word boundary when possible (first word is hard-cut)."""
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    words = [w for w in re.split(r"[^A-Za-z0-9]+", text) if w]
    out = ""
    for w in words:
        w = w[:1].upper() + w[1:]
        if len(out) + len(w) > maxlen:
            if not out:
                out = w[:maxlen]
            break
        out += w
    return out


def clean_req(req):
    """Employer req id reduced to alphanumerics and hyphens ('' when nothing is left)."""
    return re.sub(r"[^A-Za-z0-9-]+", "", str(req or "")).strip("-")


def base_name(company, role, employer_job_id, app_id):
    """`<Company>_<RoleSlug>_<ReqId>`, or `_A<app_id>` when there is no usable req id."""
    parts = [camel(company, COMPANY_MAX) or "Company", camel(role, ROLE_MAX) or "Role"]
    parts.append(clean_req(employer_job_id) or f"A{app_id}")
    return "_".join(parts)


def node_slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def index_text(name, app_id, company, role, employer_job_id, posting_id):
    one = lambda s: re.sub(r"\s+", " ", str(s or "")).strip()
    req = one(employer_job_id)
    title = f"# {one(company)}: {one(role)}" + (f" ({req})" if req else "")
    return (
        "---\n"
        f"node_id: PRO:application-{node_slug(name)}\n"
        "node_type: project\n"
        "tags: [#professional #job-search #application]\n"
        "ai_edit_policy: free-edit\n"
        f"app_id: {app_id}\n"
        f"posting_id: {posting_id or 'none'}\n"
        "---\n"
        f"{title}\n\n"
        f"> Status, dates and contacts live in the tracker database (`trk show {app_id}`); "
        "this file holds the package notes.\n"
    )


def create_folder(job_dir, *, app_id, company, role, employer_job_id=None, posting_id=None):
    """Create `<job_dir>/Applications/<name>[_2...]` with a minimal index.md.

    Never touches an existing folder: a taken name gets `_2`, `_3`, ... Returns the path relative
    to `job_dir` (forward slashes).
    """
    apps = Path(job_dir) / APPLICATIONS_DIR
    apps.mkdir(parents=True, exist_ok=True)
    base = base_name(company, role, employer_job_id, app_id)
    n = 1
    while True:
        name = base if n == 1 else f"{base}_{n}"
        try:
            (apps / name).mkdir()
            break
        except FileExistsError:
            n += 1
    (apps / name / "index.md").write_text(
        index_text(name, app_id, company, role, employer_job_id, posting_id),
        encoding="utf-8", newline="\n")
    return f"{APPLICATIONS_DIR}/{name}"


def use_folder(job_dir, rel, *, app_id, company, role, employer_job_id=None, posting_id=None):
    """An explicit `--folder <path>` (relative to the Job_Search dir): keep an existing folder as is,
    or create it with an index.md. Returns the normalised relative path."""
    p = Path(str(rel).replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise TrackerError(f"--folder must be a relative path inside the Job_Search directory, got {rel!r}")
    target = Path(job_dir) / p
    if not target.exists():
        target.mkdir(parents=True)
        (target / "index.md").write_text(
            index_text(p.name, app_id, company, role, employer_job_id, posting_id),
            encoding="utf-8", newline="\n")
    return p.as_posix()
