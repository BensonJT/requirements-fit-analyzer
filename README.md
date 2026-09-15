# jobsearch

Read job postings straight from employers' own applicant tracking systems (ATS) into a local DuckDB database, track each posting from the day it appears to the day it's taken down, and find the ones worth applying to with SQL.

## Why read the ATS directly

Job aggregators re-list postings that closed weeks ago, and their links often dead-end. LinkedIn search is noisy and hides the posting date. The employer's ATS is the only source that is always current: if a req is on the Workday or Greenhouse board, it's open, and when it disappears, it's closed.

Most large ATS platforms serve their public career sites from a JSON endpoint. This project calls those same endpoints, the ones your browser calls when you visit the careers page, for a list of employers you choose. No login, no API keys, no parsing of rendered page content. The one exception is Paylocity, whose public feed is often disabled, so the adapter reads the JSON block the all-jobs page embeds for its own script, and one description block from each job's page.

What you get:

- **A live picture of every board you care about.** One run pulls every posting from every registered employer.
- **History.** Each posting records when it was first seen, last seen, and taken down. Nothing is ever deleted.
- **A growing corpus of full job descriptions.** It's useful for term-frequency analysis (TF-IDF), resume keyword work, or tracking how an employer's hiring shifts.
- **Filtering in SQL** instead of a search box. See [Querying](#querying).

## How a run works

```mermaid
flowchart LR
    R[Registry CSV<br/>employer → ATS + ids] --> P[Pull every board<br/>8 in parallel]
    P --> U[Upsert<br/>one transaction per board]
    U --> C[Close postings missing<br/>from a clean pull]
    C --> N[Fetch JDs for NEW postings<br/>Workday · Oracle · Workable · BambooHR ·<br/>SmartRecruiters · Eightfold · Paylocity]
    N --> B[Fetch JDs for older backlog<br/>small budget, newest first]
    B --> D[(DuckDB<br/>postings)]
```

1. **Pull.** Each registered board is read in full, with no job cap. Boards run eight at a time. Pages within one board run one after another with a short delay, so no single host gets more than about one request per second.
2. **Upsert.** Every posting has a stable ID made from employer, platform, and the ATS's own req ID. A posting already in the database is updated in place. A new one is inserted with `first_seen_at` set. A board's writes go through a staging table in a single transaction.
3. **Close.** Any active posting that didn't appear in this pull is marked `status = 'closed'` with a `closed_at` timestamp. This only happens when the board was pulled cleanly and completely. A failed or truncated pull closes nothing. A closed posting that reappears is reopened.
4. **New-posting details.** Some platforms' list endpoints return only a title and a location. For those, every posting that is new this run gets its detail page fetched automatically: full description, every location, exact dates, and pay when the text states it. A safety cap (`--new-detail-cap`, default 5,000) matters only on a board's first-ever sweep, when every posting counts as new. The overflow goes to the backlog.
5. **Backlog details.** A small budget (`--detail-budget`, default 300) works through older postings still missing a description, newest first. It is prioritized by a title regex in `backend/ats/prefilter.py`. That regex decides what gets fetched *first*, never what gets stored.

A detail request that returns 404 closes the posting, so the detail stage doubles as a liveness check.

### What a run looks like

One line per board, then the detail stage, then a run summary. This is from a real run, trimmed:

```text
  [119/125] McKesson (workday): 484 live, 484 new, 0 reopened, 0 taken down — 21.5s
  [120/125] Wells Fargo (workday): 1719 live, 0 new, 0 reopened, 5 taken down — 89.5s
  [123/125] Philips (workday): 823 live, 0 new, 0 reopened, 0 taken down — 70.5s
  [125/125] CVS Health (workday): 19449 live, 18453 new, 0 reopened, 4 taken down — 663.1s
Detail stage: fetching 300 JDs (6 workers)...
  CVS Health: 77 JDs, 0 gone, 0 errors
  Novartis: 103 JDs, 0 gone, 0 errors
  Accenture: 120 JDs, 0 gone, 0 errors
Detail stage done: 300 fetched, 0 closed as gone, 0 errors.

Run b2213154 done in 17.4 min: 124 boards ok, 1 failed, 76431 live postings, 54941 new, 0 reopened, 111 taken down, 300 JDs fetched
```

`new` on a board's first sweep is every posting on it (McKesson, CVS above). On later runs it is only what appeared since the last run (Wells Fargo). `taken down` is the close pass at work. A board that fails shows up in `vw_board_health`, and nothing on it is closed.

## Supported platforms

| Platform | List endpoint gives | Detail fetch | Registry identifiers |
|---|---|---|---|
| Workday | title, one location or "N Locations", relative date | yes | tenant, data center (`wd1`, `wd5`, …), site slug |
| Oracle Recruiting Cloud | title, locations, workplace type, dates, level | yes | host URL, site number |
| Greenhouse | everything, including description | not needed | board token |
| Lever | everything, including structured salary | not needed | company slug |
| Ashby | everything, including description | not needed | org slug |
| Workable | title, locations, workplace type, date | yes | account slug |
| BambooHR | title, location, department | yes | subdomain |
| SmartRecruiters | title, location, remote/hybrid, date, level | yes | company ID (case-sensitive) |
| Eightfold | title, locations, workplace flag, date, department | yes | host URL, domain |
| Paylocity | title, location, remote flag, department, date (from the JSON block embedded in the all-jobs page) | yes | company GUID, slug |

**Not supported:**
- **iCIMS** answers scripted requests with a human-verification page.
- **Dayforce** returns 403 even with cookies and CSRF tokens.
- **Taleo** has no public JSON. Taleo Enterprise career sections increasingly redirect to a vendor front end, and Taleo Business Edition serves only rendered HTML.
- **Phenom, SuccessFactors, ADP, and UKG** have documented endpoints but no adapter yet. Rows on these platforms load but are skipped.

## Setup

Requires Python 3.11+.

```bash
git clone https://github.com/BensonJT/jobsearch.git
cd jobsearch
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q          # unit tests, no network
```

A fresh clone runs against `registry/ats_registry.example.csv`, eight example boards covering every supported platform. The ATS sweep needs no API keys. To use your own list, copy the example to `registry/ats_registry.csv`, or point `JOBSEARCH_REGISTRY_DIR` at a directory holding one. Settings go in a `.env` file in the repo root; copy `.env.template` to start, since it documents every setting both sweeps read:

```env
JOBSEARCH_REGISTRY_DIR=/path/to/your/registry
```

### First result in five minutes

Sweep three of the example boards, then ask the database what it saw:

```bash
.venv/bin/python sweep_ats.py --limit 3 --detail-budget 0
.venv/bin/python -c "
import duckdb; con = duckdb.connect('db/jobsearch.duckdb', read_only=True)
print(con.sql('SELECT employer, title, location_primary FROM vw_active ORDER BY employer, title LIMIT 15'))
print(con.sql('SELECT * FROM vw_board_health'))"
```

The first command prints one line per board within seconds, then spends a couple of minutes fetching descriptions for the Workday and Oracle postings (add `--new-detail-cap 0` to skip that). The second prints fifteen open postings and a health row per board. From here, replace the example registry with employers you care about and run without `--limit`.

## The registry

One row per employer:

```csv
employer,platform,identifier_1,identifier_2,identifier_3,live_count,source,notes
Compassion International,workday,compassion,wd5,CompassionCareers,,manual,
Planning Center,greenhouse,planningcenter,,,,manual,
```

**Finding the identifiers.** Open the employer's careers page, click into any job, and read the URL:

| URL looks like | platform | identifiers |
|---|---|---|
| `https://compassion.wd5.myworkdayjobs.com/CompassionCareers/job/...` | `workday` | `compassion`, `wd5`, `CompassionCareers` |
| `https://fa-xxxx.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/...` | `oracle_orc` | `https://fa-xxxx.fa.ocs.oraclecloud.com`, `CX_1001` |
| `https://job-boards.greenhouse.io/planningcenter/jobs/123` | `greenhouse` | `planningcenter` |
| `https://jobs.lever.co/aledade/...` | `lever` | `aledade` |
| `https://jobs.ashbyhq.com/dandy/...` | `ashby` | `dandy` |
| `https://apply.workable.com/credence/j/...` | `workable` | `credence` |
| `https://biblica.bamboohr.com/careers/64` | `bamboohr` | `biblica` |
| `https://jobs.smartrecruiters.com/ServiceNow/...` | `smartrecruiters` | `ServiceNow` |
| `https://apply.careers.microsoft.com/careers/job/123` | `eightfold` | `https://apply.careers.microsoft.com`, `microsoft.com` |
| `https://recruiting.paylocity.com/recruiting/jobs/All/cd6cba65-…/Logos` | `paylocity` | `cd6cba65-…` (the GUID), `Logos` |

**Things that trip people up:**
- **Vanity careers sites hide the ATS.** A branded site like `careers.example.com` is often a front end for Workday or Oracle. Click Apply and watch where you land.
- **A Workday tenant name isn't always the company name.** Amentum's jobs live on the `pae` tenant from a company it acquired. RTX's live on `globalhr`.
- **Workday's data center and site slug must match exactly.** A wrong data center returns 422, and a missing site slug returns 405.
- **Paylocity apply links carry only a numeric job ID.** `recruiting.paylocity.com/Recruiting/jobs/Apply/4466374` names no company. Open the job's Details page and follow its all-jobs link; the company GUID is in that URL. The public v2 feed for the GUID often returns an empty list even when jobs are live.
- **Eightfold's domain is the company's email domain, not the careers host.** Microsoft is `microsoft.com` on `apply.careers.microsoft.com`; Liberty Mutual is `libertymutual.com` on `libertymutual.eightfold.ai`. Some tenants answer only one of Eightfold's two list APIs, and the adapter tries both.

Set `source` to `unresolved` to keep a row in the file without sweeping it.

## Make it yours

Four things in this repo are tuned to the author's own search. Change them before you rely on the results:

| What | Where | Why it matters |
|---|---|---|
| The employer list | `ats_registry.csv` in your registry directory | Everything else follows from it. Start with ten employers you'd actually apply to. |
| The backlog title regex | `DETAIL_TITLE_PATTERN` in `backend/ats/prefilter.py` | Decides which older postings get their description fetched first. It currently favors operations, process, program and data titles. Put your own titles in. It never decides what is stored. |
| The commute-zone view | `vw_dmv_or_remote_active` in `backend/ats/store.py` | Matches the DC / Northern Virginia / Maryland area. Copy the view and swap in your own city and state patterns. |
| The pay and location rules | `backend/profile_local.py` (copy from `profile_local.example.py`) | Only the older aggregator sweep and rule screen read these. Gitignored, so your numbers stay local. |

### Your context (finder)

The finder ranks postings against your own background: pay and places in `backend/profile_local.py`, and what you have done in `evidence.local.toml` (resume bullets, a bio, a LinkedIn PDF, articles). Requirement coverage matches each JD requirement by meaning against that evidence. [`docs/SETUP_CONTEXT.md`](docs/SETUP_CONTEXT.md) covers what to gather and how to write evidence that matches. `finder.py setup-check` verifies the setup.

**Privacy.** Local only: the DuckDB file, evidence and embeddings, models and snapshots (all gitignored; the fit model and coverage run offline). Gemini free tier (optional Phase 4 `public` prompts): JD requirement units, the public rubric and a neutral role description, never your evidence. Gemini with billing, or the Claude Code batch files: also the personal rubric and claim guards, still never evidence text.

## Usage

```bash
.venv/bin/python sweep_ats.py                                   # full run
.venv/bin/python sweep_ats.py --employer "capital one"          # one employer (substring match)
.venv/bin/python sweep_ats.py --platform workday                # one platform
.venv/bin/python sweep_ats.py --limit 10                        # first 10 boards, for a smoke test
.venv/bin/python sweep_ats.py --skip-sweep --detail-budget 5000 --detail-all   # backfill JDs only
```

| Flag | Default | What it does |
|---|---|---|
| `--workers` | 8 | boards pulled in parallel |
| `--new-detail-cap` | 5000 | max JD fetches for postings new this run, all titles (0 = off) |
| `--detail-budget` | 300 | JD fetches for the older backlog (0 = off) |
| `--detail-all` | off | ignore the title prefilter for the backlog |
| `--max-pages` | none | stop a board after N pages; that board skips its close pass |
| `--skip-sweep` | off | run only the backlog detail stage |
| `--db` | `db/jobsearch.duckdb` | database path |

To run it daily, schedule the full run with cron, Task Scheduler, or systemd:

```cron
0 6 * * * cd /path/to/jobsearch && .venv/bin/python sweep_ats.py >> output/daily.log 2>&1
```

## Querying

Open the database with the [DuckDB CLI](https://duckdb.org/docs/installation/) (`duckdb db/jobsearch.duckdb`) or from Python. Close any other connection first, because a running sweep holds the write lock.

Parameterized table macros take a look-back window or a regex:

```sql
SELECT employer, title, location_primary FROM new_postings(1);            -- first seen in the last day
SELECT employer, title, location_primary FROM posted_within(7);           -- posted in the last week, by the ATS's own date
SELECT employer, title, days_visible   FROM taken_down(7);                -- closed in the last week
SELECT employer, title FROM title_match('operational excellence|process improvement|six sigma');
SELECT employer, title FROM title_match_new('program manager', 3);       -- title regex, new in the last 3 days
SELECT employer, title FROM text_match('black belt|lean');                -- regex over title + full description
```

Views:

| View | Contents |
|---|---|
| `vw_active` | everything currently open |
| `vw_remote_active` | open and remote, by flag or location text |
| `vw_dmv_or_remote_active` | open and remote, or in the DC / Northern Virginia / Maryland area (an example of a commute-zone view; copy it for your own region) |
| `vw_pay_annualized` | postings with pay, hourly converted at × 2,000 |
| `vw_board_health` | each board's latest pull: ok or failed, job count, error |
| `vw_posting_lifetimes` | average days a posting stays up, per employer |

Macros and views compose:

```sql
SELECT n.employer, n.title, n.location_primary, p.annual_min, p.annual_max
FROM title_match_new('director|principal', 7) n
JOIN vw_remote_active USING (posting_id)
LEFT JOIN vw_pay_annualized p USING (posting_id)
ORDER BY p.annual_max DESC NULLS LAST;
```

## Schema

All platforms normalize into one `postings` table. Every definition lives in `backend/ats/store.py`.

| Group | Columns |
|---|---|
| identity | `posting_id`, `employer`, `platform`, `req_id`, `title`, `url` |
| location | `location_primary`, `locations` (JSON array), `country`, `workplace_type` (`remote` / `hybrid` / `onsite`) |
| role | `employment_type`, `job_family`, `job_level` |
| pay | `pay_min`, `pay_max`, `pay_currency`, `pay_interval` (`year` / `hour`), `pay_source` (`ats` or `text`) |
| dates from the ATS | `posted_at`, `posting_end_at` |
| corpus | `description_text`, `description_hash`, `description_fetched_at`, `raw_json` |
| lifecycle | `first_seen_at`, `last_seen_at`, `closed_at`, `status` |
| screening (reserved) | `screen_verdict`, `screen_score`, `screen_reasons`, `screened_at` |

`raw_json` keeps each platform's original list record, so a field that was mapped badly can be re-derived without re-fetching. `runs` and `board_runs` log every run and every board pull.

## Performance

Measured on a WSL2 laptop with the database on an external drive, which is the slow case:

| | |
|---|---|
| Full sweep, 132 boards, ~77,000 postings | 17 minutes |
| Detail fetches, 6 boards in parallel | ~75 per minute |
| Database size at ~77,000 postings with ~13,000 descriptions | under 1 GB |

The first sweep of a large Workday or Oracle board is the expensive part, because every posting is new and needs a detail fetch. After that, a daily run only fetches descriptions for that day's new postings.

## Limitations

- **Only the employers you register.** It will not discover a company you've never heard of. Pair it with a job board for discovery.
- **Adapters depend on undocumented endpoints.** They are the endpoints the vendors' own career sites use, and they have been stable, but a vendor can change one without notice. Check `vw_board_health` after each run.
- **`first_seen_at` is when *you* started watching, not when the job was posted.** The day you add a board, every posting on it is new, so `new_postings(1)` will look like a hiring spree. Use `posted_at`, or `posted_within(days)`, for the job's real age. After the first sweep the two agree.
- **Eightfold pages shift under a long pull.** The board is read ten postings at a time, and an employer refreshing a posting mid-pull moves it to the top and shifts every page below. The adapter reads the board in two orders and unions them. If the union is still more than half a percent short of the board's own count, the pull is marked truncated and closes nothing.
- **Workday list dates are relative.** "Posted 3 Days Ago" is converted against the run date. "Posted 30+ Days Ago" stays empty until the detail fetch supplies the real date.
- **Workday and Oracle rows are thin until their detail fetch runs.** Before that, a multi-site Workday posting has no location, and `workplace_type` is usually empty. Views that filter on location will miss those rows.
- **Pay is often parsed from description text.** When the ATS has no salary field, a conservative regex pulls the first dollar range from the description. `pay_source` says which kind you're looking at. Treat `text` values as hints.
- **Workday's keyword search isn't used.** It matches words loosely and can't combine phrases, so filtering happens in SQL after a full pull.
- **Errors can hide as empty boards.** A wrong SmartRecruiters company ID, or an empty Greenhouse board, returns success with zero jobs, which looks like a board with nothing open.
- **One writer at a time.** DuckDB allows one process to hold the write lock. Don't run two sweeps at once, and close your query session before a scheduled run.
- **Some platforms block scripts entirely.** iCIMS and Dayforce can't be read this way, and neither can career sites behind a bot firewall. For those, find the underlying ATS, or visit by hand.

## Responsible use

This reads public job listings that employers publish so candidates can find them. It sends no credentials and fills out no forms. It paces requests per host and reads each board once per run. Keep it that way. Run it at most daily, keep the per-host delay, and don't point it at sites that block automated access. You are responsible for complying with each site's terms.

## Adding a platform

The unsupported list above is where help is most useful. An adapter is two functions in `backend/ats/adapters.py`:

- **`<platform>_jobs(row, max_pages=None)`** reads the whole board for one registry row and returns a list of postings built with `normalize.base(...)`. It must raise on a hard failure, never return a partial list as if it were complete, so the close pass can trust it. If `max_pages` stops it early, return a `Truncated` list.
- **`<platform>_detail(row, posting)`**, only if the list endpoint is thin, fetches one posting's full record and raises `Gone` on 404.

Register the list function in `_LIST` and the detail function in `_DETAIL`, add a row to the platform table in this README, and add a test in `tests/test_ats.py` that feeds a saved JSON response through the adapter with no network. `normalize.py` has the helpers for dates, workplace type, pay parsing and HTML to text, so a new adapter is mostly field mapping. The Greenhouse adapter is the shortest example to copy from.

Before writing one, open the platform's public careers page with the browser's network tab open and confirm it serves JSON to an anonymous request. If it returns a bot check or 403, as iCIMS and Dayforce do, there is nothing to adapt.

If you resolve an employer's identifiers and want to share them, open an issue with the careers URL and the platform. Registry rows are cheap to add and hard to find.

## Also in this repo

`sweep.py` is an earlier aggregator sweep over Adzuna, Jooble, and USAJobs. Their free API keys go in `.env`, and any source without a key is skipped. It is paired with a rule-based screen, `backend/screen.py`, driven by `backend/profile.py`. The profile holds the author's own title, pay, and location rules. Replace them with yours before using it. Aggregator results are stale often enough that anything it finds should be confirmed on the employer's own site.

`docs/STATUS.md` and `CLAUDE.md` are the author's working notes and the working agreement for the AI coding assistant used on this repo. They describe the author's machines and current session, not the product. Read this README instead.

## Project layout

```
sweep_ats.py              ATS sweep CLI
backend/ats/
  registry.py             loads the registry CSVs
  adapters.py             one list_jobs() and, where needed, fetch_detail() per platform
  normalize.py            dates, workplace type, pay, HTML to text: one shape for every platform
  prefilter.py            title regex that orders the backlog detail stage
  store.py                DuckDB schema, migrations, upsert and close logic, views and macros
  sweep.py                orchestration: parallel pulls, main-thread writes, detail stages
registry/                 example registry
tests/test_ats.py         unit tests (no network)
LICENSE                   MIT
db/                       DuckDB file lives here (gitignored)
sweep.py, backend/screen.py, backend/profile.py   aggregator sweep + rule-based screen
```
