# Jobsearch — Claude Working Agreement

## Active Memory Files (loaded at session start)
@/docs/STATUS.md

## North Star (do not optimize away)
Jobsearch simplifies and automates the process of finding and applying for jobs,
leveraging intelligent search and application tracking to help the user manage
their career growth efficiently.

## Current Scope
Two sweeps. `sweep_ats.py` reads employer ATS boards into DuckDB (the main engine). `sweep.py` is the older aggregator pre-screen with a rule engine (`backend/screen.py` + `backend/profile.py`). No LLM call anywhere.
- **`sweep.py`** (aggregator discovery — Adzuna, Jooble, USAJobs): stable, in regular use.
  Replaced the original March-2026 `main.py` harvester's keyword list. Every result gets a
  live-verification step against the employer's own site before it's trusted — see
  `docs/STATUS.md` for why (every Adzuna/Jooble link the 2026-09-14 run followed 403'd).
- **`sweep_ats.py`** (ATS-direct — `backend/ats/`): the daily engine. Whole-board pulls from
  Workday, Oracle ORC, Greenhouse, Lever, Ashby, Workable, BambooHR, SmartRecruiters, Eightfold and Paylocity, upserted into `db/jobsearch.duckdb`
  (never re-added; closed when they disappear; never deleted), plus a budgeted per-posting JD
  detail stage. Filtering is done in SQL views/macros defined in `backend/ats/store.py`.
  Design: vault `Professional/Areas/Job_Search/Tools/DESIGN_ats_registry.md` (§14 = this build).
- The original March-2026 `main.py` harvester was deleted 2026-09-15 (git history has it).
- Frontend/dashboard not started, not currently planned.

## Repo map (where to look first)
- **`sweep.py`** — aggregator sweep entry point (current, in use)
- **`sweep_ats.py`** — ATS-direct sweep entry point (current, being timed)
- `backend/profile.py` — title/exclusion rules; personal pay + location live in gitignored `backend/profile_local.py`
- `backend/screen.py` — the rule engine both sweeps call
- `backend/ats/registry.py` — loads the vault's `ats_registry.csv` (single file since 2026-09-15)
- `backend/ats/adapters.py` — `list_jobs(row)` + `fetch_detail(row, posting)` per platform
- `backend/ats/normalize.py` — one shape for every platform's fields
- `backend/ats/store.py` — DuckDB schema v2, transactional upsert/close, views + macros
- `backend/ats/sweep.py` — orchestration (concurrent pulls, detail budget)
- `tests/test_ats.py` — run with `.venv/bin/python -m pytest -q`
- `db/` — the DuckDB job store (gitignored contents; see `db/README.md`)
- `docs/STATUS.md` — session state (read first, update last)

## Local environment
**Vostro is primary as of 2026-09-14** (it's on all the time; OptiPlex isn't). Work happens
on Vostro; OptiPlex is a backup checkout, synced by Vostro pushing to it over SSH (`git push
optiplex-backup main:refs/heads/from-vostro`, then `ssh optiplex "cd ~/code/jobsearch && git
merge --ff-only from-vostro && git branch -d from-vostro"` — OptiPlex's repo is non-bare with
`main` checked out, so pushing straight to `main` there is refused; push to a side branch and
fast-forward-merge it instead). This is one-directional (Vostro → OptiPlex) because OptiPlex
can reach Vostro's `optiplex` SSH host, but not the reverse — Vostro runs inside WSL2 behind
NAT and isn't reachable from the LAN.

- **Vostro** (primary): `/mnt/e/code/jobsearch` (via the `~/code` symlink to the E: drive —
  deliberately off C:, which is nearly full). Local venv at `.venv/` — `python-dotenv`,
  `httpx`, `duckdb`, `beautifulsoup4`, `lxml`, `pytest` (see `requirements.txt`). DuckDB job
  store lives in `db/jobsearch.duckdb` (gitignored; see `db/README.md`).
- **OptiPlex** (backup): Xubuntu Linux, conda env `jobsearch`, Python 3.12+. Still runnable
  standalone (`source ~/miniconda3/etc/profile.d/conda.sh && conda run -n jobsearch python
  main.py --keywords ... --location Remote`), but treat it as a mirror, not the place new
  work starts.

## Session continuity (STATUS.md)
`docs/STATUS.md` is the session state file — read it first, update it last.
- **Start:** Output the SESSION START CONFIRMATION block before doing anything else
- **End:** Overwrite STATUS.md with current state snapshot (git history is the changelog)
- Do not wait to be asked — updating STATUS.md is part of wrapping up, same as a commit
- Keep it concise — bullet points preferred over prose

## Sprint Plan (when active)
If `docs/SPRINT_PLAN.md` exists, it is a binding contract.
- Execute tasks in checklist order — do not reorder without explicit user confirmation
- Write the finalized plan to `docs/SPRINT_PLAN.md` using the established structure
- Restore `@/docs/SPRINT_PLAN.md` in the Active Sprint section of `docs/STATUS.md`

## When uncertain
- Ask a single focused question, then proceed with a reasonable default.

## Coding standards
- Python: follow PEP 8; use type hints where practical; keep functions readable;
  add concise comments/docstrings.
- Prefer "boring, maintainable" implementations over novel ones.

## Git commits:
- After completing any meaningful unit of work, commit locally with a
  descriptive commit message.
- Commit message format:
  - First line: short summary in imperative mood, max 72 chars
  - Blank line
  - Body: 2-5 bullets explaining what changed and why
- Group related changes into one commit.
- Do NOT push to GitHub — the user handles all pushes.

## Public repo rules (added 2026-09-15)
This repo is public on GitHub (`origin` = git@github.com:BensonJT/jobsearch.git).
- **Never commit personal data**: pay figures, home location, vault paths, names. Those go in
  gitignored `.env` (`JOBSEARCH_REGISTRY_DIR`, `JOBSEARCH_VAULT_DIR`) and `backend/profile_local.py`.
- Before any commit, run: `git grep --untracked -n -i -E -f .personal_patterns -- . ':!.personal_patterns' ':!LICENSE'` — must be empty. `.personal_patterns` is gitignored and holds the terms that must never appear.
- The registry CSVs live outside the repo (vault); `registry/ats_registry.example.csv` is the public sample.
