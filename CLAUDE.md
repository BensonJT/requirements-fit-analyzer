# Jobsearch — Claude Working Agreement

## Active Memory Files (loaded at session start)
@/docs/STATUS.md

## North Star (do not optimize away)
Jobsearch simplifies and automates the process of finding and applying for jobs,
leveraging intelligent search and application tracking to help the user manage
their career growth efficiently.

## Current Scope
Two sweeps. `sweep_ats.py` reads employer ATS boards into DuckDB (the main engine). `sweep.py` is the older aggregator pre-screen with a rule engine (`backend/screen.py` + `backend/profile.py`). No LLM call unless `--llm-top` is passed. The finder layer (`backend/finder/`, `finder.py`) scores the corpus and runs as the last stage of `sweep_ats.py`.
- **`sweep.py`** (aggregator discovery — Adzuna, Jooble, USAJobs): stable, in regular use.
  Replaced the original March-2026 `main.py` harvester's keyword list. Every result gets a
  live-verification step against the employer's own site before it's trusted — see
  `docs/STATUS.md` for why (every Adzuna/Jooble link the 2026-09-14 run followed 403'd).
- **`sweep_ats.py`** (ATS-direct — `backend/ats/`): the daily engine. Whole-board pulls from
  Workday, Oracle ORC, Greenhouse, Lever, Ashby, Workable, BambooHR, SmartRecruiters, Eightfold, Paylocity, Deel ATS, Phenom, SAP SuccessFactors RMK (`successfactors_rmk`) and Avature, upserted into `db/jobsearch.duckdb`
  (never re-added; closed when they disappear, or when the employer moves to a new board; never deleted), plus a budgeted per-posting JD
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
- `backend/ats/store.py` — DuckDB schema v3 (finder tables added), transactional upsert/close, views + macros
- `backend/ats/sweep.py` — orchestration (concurrent pulls, detail budget, then the finder stage)
- **`finder.py`** — finder CLI: screen, rescreen-all, report, top, mark, apply-queue, sync, shortlist, labels, train, evidence, coverage, setup-check.
  `mark`'s `--unmet "<JD line>"` (repeatable, with `--reason "requirement: ..."` or `"clearance: ..."`) records a
  human Required-block miss that outranks the judge everywhere; `mark <target> build` defaults to Required
  `meets` unless `--unmet` says otherwise. `--grade bullseye|adjacent|stretch|wrong` + `--basis blind|seen`
  records a human lane grade the same way the golden feedback CSV import does. `mark --from-file decisions.csv`
  applies a whole review session (columns `posting,decision,reason,unmet,grade,basis`) in one transaction,
  validated in full first. A logistics/comp pass is never a fit negative for any model, even graded.
  `feedback sheet --n 20 [--seed S] [--out PATH]` writes the monthly blind sheet with decoys (sprint plan
  §26): a score-free CSV (8 top-50-by-Rank rows, 6 rank-200-600 rows, 6 screen rejects split location/
  clearance/title, one per employer+title, shuffled) plus a sidecar JSON recording each row's stratum for
  later scoring. `feedback sheet-import PATH` writes the graded rows back as `basis='blind'` through the
  same path the golden-source CSV import uses, and prints precision at the top / the 200-600 miss rate /
  the false-reject rate per rule bucket, appended to `db/blind_sheet_history.jsonl`.
- **Decision queue** (`db/pg/005`-`007`, `010`; `jobs.decision_queue`): build / pass / hold calls made while a run holds the DuckDB. `scripts/feedback_app.py` (tkinter; `scripts/feedback_app.vbs` launches it from Windows) queues them and changes tracker status through `trk set`; `finder.py apply-queue` replays them with `mark`'s checks (decided_at = submitted_at, source `queue`); per-lens grades (seen or blind) go to `human_lens_grades` through gold_ingest's F4 path in the same transaction. The applet also fills the overall `grade` on any call; a `grade` (Grades only) row carries grades with no decision, plus an optional `required_fit` (meets | fails, 010) that apply-queue writes with its overall grade to `report_feedback` through `feedback.record_mark` (no `decisions` row; neutral verdict), so a blind one is a gold row for judge2 / Jev; run by `launch.sh` at the start and before the snapshot and hourly by `scripts/inbox_watch.sh`. Rows are kept: applied_at / apply_error / cancelled_at.
- `backend/tracker/`: the application tracker in PostgreSQL (`resume` DB, schemas `tracker` + `jobs`). `store` is the only writer and logs one event per write; `render` writes the markdown view; `gsheet` writes the one-way Google Sheet view; `cli` is `bin/trk` (`trk --help` is the command reference). The design lives in the user's vault.
- `backend/finder/` — rules (JD-text rules on top of `screen.py`), pipeline (screen + combine + daily), tracker_sync, report (Jobs_Found + snapshots + decision read-back), labels (vault label loaders → `label_docs`), features (TF-IDF + logistic regression fit model; `db/models/`, gitignored), evidence (manifest `evidence.local.toml` → `evidence_units`), requirements (JD → requirement units), embed (bge-small encoder), coverage (requirement coverage + calibration; Phase 3a = stored and shown, weight 0), required_embed (the "second layer" stacked model: TF-IDF + bge-base Required-block embedding + per-line roll-up -> `embed_required`, own `required_embed` table, NOT a lens), blind_sheet (the monthly blind sheet with decoys, §26), setup_check. scikit-learn and the embedding libraries are optional and imported lazily. Context setup for any user: `docs/SETUP_CONTEXT.md`
- `tests/test_ats.py`, `tests/test_finder.py`, `tests/test_blind_sheet.py` — run with `.venv/bin/python -m pytest -q`
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

- **Vostro** (primary; both machines' paths are also in keystone
  `System/Context/Skills/Environment_Map.md`): **`~/jobsearch` on the WSL ext4 disk, since 2026-09-16.** The E: copy
  (`~/code/jobsearch` → `/mnt/e/code/jobsearch`) is RETIRED: `/mnt/e` is a 9p mount served from
  Windows, and torch/transformers imports over it failed under Windows memory pressure (bus
  error, ENOMEM on `open()`, one WSL crash). Local venv at `.venv/` (Python 3.14, CPU torch,
  sentence-transformers; packages in `requirements.txt`, no pinned freeze on disk — the old
  `~/jobsearch_native/freeze_clean.txt` is gone; `.venv/bin/pip freeze` makes a new one). DuckDB job
  store lives in `db/jobsearch.duckdb` (gitignored; see `db/README.md`). Gitignored files that a
  clone does NOT bring: `.env` (incl. `RESUME_DB_URL`), `evidence.local.toml`,
  `.personal_patterns`, `backend/profile_local.py`, `backend/finder/rubric_local.py`, `db/`.
  Caveat: the ext4 disk is a VHDX on C:, which is nearly full — keep large backups on E:.
- **OptiPlex** (backup): Xubuntu Linux, conda env `jobsearch`, Python 3.12+. Still runnable
  standalone (`source ~/miniconda3/etc/profile.d/conda.sh && conda run -n jobsearch python
  main.py --keywords ... --location Remote`), but treat it as a mirror, not the place new
  work starts.

## Full pipeline order (the report is always last)
`sweep_ats.py` runs the stages in this order and writes the vault report at the end: sweep → JD details → tracker
sync → decision read-back → screen → **coverage** → **required-embed** → (LLM, Phase 4) → **`Jobs_Found_*.md` +
snapshots**. `required-embed` runs after coverage because it depends on `requirement_units`; it never fails the
sweep (no trained model / no encoder library = one log line, skipped). Run by hand in the same order, and never
stop before the report — a run without it leaves the new screens invisible in the vault:
```bash
finder.py labels --report && finder.py train --report && finder.py rescreen-all && finder.py sync \
  && finder.py evidence --rebuild && finder.py coverage && finder.py required-embed score && finder.py report
```
(`finder.py required-embed train --report` is a separate, occasional step -- retrain only when the judged corpus
has moved meaningfully, not every run.)

**Weekly retrain (sprint plan §24):** `finder.py retrain [--dry-run]` replaces hand-running `labels` + `train`
per lens/`required`/`bullseye` + `required-embed train`. Each candidate is gated (employer-grouped held-out
AUC vs. the previous PROMOTED run, a shuffled-label leak check, label count not falling more than 5%) before it can
replace the live artifact; a failure keeps the old one and names the failed gate with numbers. Rescreen /
coverage / `required-embed score` only run if something was promoted. `finder.py retrain --history` reads
the `model_runs` ledger back, newest first. `sweep_ats.py` prints (never fails on) a one-line reminder when
the newest promoted run is stale (see README).
`finder.py report` excludes postings already surfaced as blocks in the last 14 days (`surfaced` table), so a second
report the same day shows the next rows down, while its summary table still lists everything at or above the bar.

## Session continuity (STATUS.md)
`docs/STATUS.md` is the session state file — read it first, update it last. It is git-ignored (local only, since 2026-10-24): it names employers and the job search, so it never goes to the public repo. Update it, but never `git add -f` it.
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
- **`--help` is the command reference; keep it current.** After changing or improving a command-line tool
  (`trk`, `finder.py` and its subcommands such as `mark`, `sweep_ats.py`, `scripts/*.py`), compare its
  `--help` with the code before committing: every switch has a help line, choices and defaults match,
  and the guide or epilog (`trk`'s `GUIDE`, `mark`'s `MARK_GUIDE`) covers any new side effect or failure
  mode (a lock, a skipped step). The vault skills point to `--help` instead of copying it, so a stale
  help line is a stale manual.

## Git commits:
- After completing any meaningful unit of work, commit locally with a
  descriptive commit message.
- Commit message format:
  - First line: short summary in imperative mood, max 72 chars
  - Blank line
  - Body: 2-5 bullets explaining what changed and why
- Group related changes into one commit.
- Push to GitHub after committing (user's standing go, 2026-10-19): run the `.personal_patterns` check first, and never push a commit that fails it.

## Public repo rules (added 2026-09-15)
This repo is public on GitHub (`origin` = git@github.com:BensonJT/jobsearch.git).
- **Never commit personal data**: pay figures, home location, vault paths, names. Those go in
  gitignored `.env` (`JOBSEARCH_REGISTRY_DIR`, `JOBSEARCH_VAULT_DIR`) and `backend/profile_local.py`.
- Before any commit, run: `git grep --untracked -n -i -E -f .personal_patterns -- . ':!.personal_patterns' ':!LICENSE'` — must be empty. `.personal_patterns` is gitignored and holds the terms that must never appear.
- The registry CSVs live outside the repo (vault); `registry/ats_registry.example.csv` is the public sample.
