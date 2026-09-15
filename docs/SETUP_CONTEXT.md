# Setting up your context

The finder ranks postings against **you**. Everything that describes you lives in four local files that git ignores. The code never names a person, an employer or a path.

| File | Copy from | What it holds |
|---|---|---|
| `.env` | `.env.template` | paths (`JOBSEARCH_VAULT_DIR`, `JOBSEARCH_REGISTRY_DIR`), API keys, rate limits |
| `backend/profile_local.py` | `backend/profile_local.example.py` | pay floor and ask, commutable places, travel and team-size limits |
| `backend/finder/rubric_local.py` | `backend/finder/rubric.py` (Phase 4) | a short prose profile for the optional LLM review |
| `evidence.local.toml` | `evidence.example.toml` | what you have actually done: the evidence requirement coverage matches against |

Run `.venv/bin/python finder.py setup-check` after each change. It says what is missing, what to install, and whether any personal file is not gitignored.

## What to gather

- **Achievements** (the most useful source): one accomplishment per row or bullet, as CSV or markdown. Kind `achievement`.
- **Role summaries or duty statements**, one per role. Kind `duty`.
- **A bio or a LinkedIn PDF export** (`More → Save to PDF` on your profile). Kind `narrative`.
- **Articles, a portfolio site, a method write-up.** Kind `method` for how-you-work writing, `narrative` for about pages.

**The minimum setup** is one resume PDF as a `narrative` source. Coverage gets much better once you add achievement bullets.

## How to write evidence that matches well

Coverage compares each JD requirement with each evidence unit by meaning (sentence embeddings), one sentence-sized unit at a time.

- **One accomplishment per unit.** A paragraph covering three projects matches none of them well.
- **Include the context.** "Order handling ran through five teams with no single owner" is what a JD that asks for "cross-functional process ownership" is looking for.
- **Make the verb and the object explicit.** "Mapped the order-to-delivery value stream" matches; "Drove results across the org" matches everything weakly and nothing strongly.
- **Keep negations out of evidence.** Embeddings read "I have not used Tool X" as being about Tool X. Put such facts in `not_in_record` instead, or exclude the section with `skip_headings`.
- **Leave out meta text.** Notes to yourself, usage instructions, or coaching notes inside your documents can be dropped per source with `skip_headings = [...]` and `skip_patterns = ["regex", ...]`.

## Three tiers of record

```toml
not_in_record = ["Tool X", "Certification Y"]   # a requirement naming one is always a gap
light_in_record = ["Cloud Z"]                   # touched it, not an expert: at most a partial match
```

Terms match whole words, case-insensitively. List tools and certifications you do not hold, so that an article *mentioning* them never counts as having them.

## Source types

| type | reads | unit |
|---|---|---|
| `csv` | `text_columns` joined per row; `ref_columns` shown as the match reference | one row (long rows split at sentence ends) |
| `markdown` | a file or a folder (`include` / `exclude` globs); frontmatter and code fences dropped | paragraph, list item, table row; the heading is the reference |
| `pdf` | `pdftotext` in reading order (install `poppler-utils`), else `pypdf` | paragraph |
| `html` | visible text; nav, header, footer, script and style dropped | paragraph or list item |
| `text` | plain text | paragraph |

Units are 40–600 characters. Longer ones split at sentence ends, shorter ones merge with the next unit under the same heading. The same text appearing in two sources is kept once, at the higher kind weight. `[[guard]]` files (claims you must never make) are never embedded; only the Phase 4 review prompts read them.

## Commands

```bash
.venv/bin/python finder.py evidence --check      # every source: found, unit count, three samples
.venv/bin/python finder.py evidence --rebuild    # embed new units (first run downloads the ~130 MB model)
.venv/bin/python finder.py coverage              # cover new / changed screen survivors
.venv/bin/python finder.py coverage --calibrate  # thresholds against near misses (see below)
.venv/bin/python finder.py shortlist --days 30   # `req` and `role` columns show coverage
```

**Calibration** needs near misses: postings you read and judged the wrong function. Mark them with `finder.py mark <id> pass --reason "function: <why>"`, or list their posting ids in `AUDIT_NEGATIVES` in `profile_local.py`. The calibration also adds the fit model's most confident unlabeled postings that have no function term in the title, and prints them so you can promote a real fit with `mark <id> build`.

## Privacy

- **Local only:** the DuckDB file, evidence text and embeddings, requirement units, trained models and snapshots stay on your machine, all gitignored. The fit model and coverage run offline once the embedding model is downloaded.
- **Gemini API, free tier (Phase 4 `public` profile):** sends the JD's requirement units, the public rubric and a neutral role description. Google may retain and review free-tier traffic. Never sent: your evidence, the personal rubric, your claim guards.
- **Gemini API with billing enabled, or the Claude Code batch path (`personal` profile):** also sends the personal rubric and the claim guards. Batch files stay on disk under `db/batches/`. Evidence text is never sent on any path.
