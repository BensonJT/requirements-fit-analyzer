# Overnight two-lens re-grade — how to run it in parallel

**Staged:** `db/batches_2lens/`, 118 batches, 2,946 postings, rubric `897123f3fc93`. ~76k tokens per batch, so ~9.0M total.

## The partition rule (so two terminals never collide)

- **Terminal A (this session, the Max account): ascending from `batch_001`.**
- **Terminal B (a second terminal on the Pro account): DESCENDING from `batch_118`.**

They meet in the middle and can never grade the same batch. Each batch writes its own result file the moment it finishes, so there is nothing to coordinate and nothing is lost if either side stops.

**Check what is left at any time — this is the only source of truth:**
```bash
cd /home/bensonjt/code/jobsearch
.venv/bin/python finder.py judge status --dir db/batches_2lens
ls db/batches_2lens/*.result.json | wc -l          # batches done
```

## Prompt for Terminal B (paste verbatim after `/login` to the Pro account)

> Read `/home/bensonjt/code/jobsearch/docs/OVERNIGHT.md`. You are **Terminal B**: grade batches **descending from batch_118** in `/home/bensonjt/code/jobsearch/db/batches_2lens/`. Before starting, run `ls db/batches_2lens/*.result.json` and skip any batch that already has a result file. Launch 8 Sonnet subagents at a time, each with this task:
>
> "Read `/home/bensonjt/code/jobsearch/db/batches_2lens/batch_NNN.md` in full. Grade EVERY posting against the rubric. Each posting gets TWO independent grades: grade_process and grade_technical. Write `/home/bensonjt/code/jobsearch/db/batches_2lens/batch_NNN.result.json` as a JSON array of {posting_id, grade_process, grade_technical, lane, confidence, blocker, rationale}. Write ONLY the JSON array, no markdown fences, no commentary. Every posting_id in the batch must appear exactly once, spelled exactly as in the file. Grade the two lenses INDEPENDENTLY — a lens with no relevant content in the posting is `wrong` on that lens, not `stretch`. Reply with ONLY the count and a two-line tally: process grades, then technical grades."
>
> Keep launching replacements as they finish. Do not run `judge import`, do not touch the DuckDB database, do not commit — Terminal A owns those. Just produce result files.

**Terminal B must not touch the database.** DuckDB takes an exclusive lock; two writers will fail. Result files are plain JSON on disk and are safe to write in parallel.

## When grading stops (Terminal A only)

```bash
cd /home/bensonjt/code/jobsearch
.venv/bin/python finder.py judge import --dir db/batches_2lens --scorer claude-sonnet-batch   # safe to re-run
.venv/bin/python finder.py judge report --csv db/snapshots/llm_labels.csv
.venv/bin/python -c "import sys;sys.path.insert(0,'.');from backend.ats import store;c=store.connect();print(dict(c.execute('SELECT lens_bucket, count(*) FROM vw_lens_grades GROUP BY 1').fetchall()))"
```

Only after the corpus is fully re-graded (mixed rubrics until then, see STATUS.md):
```bash
.venv/bin/python finder.py labels --report && .venv/bin/python finder.py train --report \
  && .venv/bin/python finder.py rescreen-all && .venv/bin/python finder.py sync && .venv/bin/python finder.py report
```

## Notes

- `--relabel` is idempotent: re-exporting to a NEW directory queues only postings whose newest label predates `897123f3fc93`. The batch directory is recoverable, not precious.
- Do not read a mid-run tally as a corpus estimate. The queue is spread proportionally by old grade (`_spread()`), so a partial run is representative — but single rows vary run to run (sprint plan §18.7).
