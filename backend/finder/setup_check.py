"""`finder.py setup-check`: is this checkout configured, and does anything personal risk being committed?
(sprint plan §15.6). Prints one line per check; returns False when something blocks."""
import importlib.util
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from backend.ats import store

REPO = Path(__file__).resolve().parents[2]
ENV_KEYS = {"JOBSEARCH_VAULT_DIR": "vault reports, tracker sync and labels",
            "JOBSEARCH_REGISTRY_DIR": "your ATS registry (else the example registry)",
            "GEMINI_API_KEY": "Phase 4 LLM review (optional)"}
PRIVATE_PATHS = (".env", "backend/profile_local.py", "backend/finder/rubric_local.py", "evidence.local.toml",
                 "db/batches/", "db/models/", "db/snapshots/", "db/jobsearch.duckdb")
OPTIONAL = (("sklearn", "fit model", ".venv/bin/pip install scikit-learn numpy scipy joblib"),
            ("bs4", "html evidence", ".venv/bin/pip install beautifulsoup4 lxml"))


def _has(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def run(con, manifest: Optional[str] = None, log=print) -> bool:
    blocking = []
    env = REPO / ".env"
    log(f"{'ok' if env.exists() else 'warn':<6} .env {'found' if env.exists() else 'missing (copy .env.template)'}")
    for key, use in ENV_KEYS.items():
        log(f"{'ok' if os.getenv(key) else 'warn':<6} {key} {'set' if os.getenv(key) else 'unset'} — {use}")
    for mod, example in (("backend.profile_local", "backend/profile_local.example.py"),
                         ("backend.finder.rubric_local", "backend/finder/rubric.py (Phase 4)")):
        found = _has(mod)
        log(f"{'ok' if found else 'warn':<6} {mod} {'imports' if found else f'not found — copy {example}'}")

    for mod, use, hint in OPTIONAL:
        log(f"{'ok' if _has(mod) else 'warn':<6} {mod} ({use}) {'installed' if _has(mod) else '— ' + hint}")
    from . import embed
    backend = embed.available()
    log(f"{'ok' if backend else 'warn':<6} embeddings {backend or '— ' + embed.INSTALL_HINT}")

    from . import evidence
    mp = evidence.manifest_path(manifest)
    if not mp.exists():
        log(f"warn   evidence manifest {mp} missing — coverage is skipped (copy evidence.example.toml)")
    else:
        try:
            m = evidence.load_manifest(str(mp))
            if any(s.type == "pdf" for s in m.sources) and not (shutil.which("pdftotext") or _has("pypdf")):
                blocking.append("a pdf source needs pdftotext (poppler-utils) or `pip install pypdf`")
            if not evidence.check(m, log=lambda line: log("       " + line)):
                blocking.append(f"evidence manifest {mp}: a source path is missing")
            else:
                log(f"ok     evidence manifest {mp}")
        except Exception as exc:  # a broken manifest is a blocking finding, not a crash
            blocking.append(f"evidence manifest {mp}: {exc}")

    version = con.execute("SELECT max(version) FROM schema_info").fetchone()[0]
    if version != store.SCHEMA_VERSION:
        blocking.append(f"DB schema version {version}, code expects {store.SCHEMA_VERSION}")
    else:
        log(f"ok     DB schema version {version}")

    if (REPO / ".git").exists() and shutil.which("git"):
        for rel in PRIVATE_PATHS:
            ignored = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", rel]).returncode == 0
            if not ignored:
                blocking.append(f"{rel} is not gitignored (personal data could be committed)")
        log(f"ok     gitignore covers {', '.join(PRIVATE_PATHS)}" if not any("gitignored" in b for b in blocking) else "")
    for b in blocking:
        log(f"BLOCK  {b}")
    log("setup-check: " + ("ok" if not blocking else f"{len(blocking)} blocking issue(s)"))
    return not blocking
