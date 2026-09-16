# Coverage experiments — does requirement coverage rank?

One record per run, newest last. Every run is `finder.py coverage --calibrate`: thresholds are grid-searched to maximize the AUC of positives against hard negatives, then the AUCs below are reported at the chosen thresholds. **AUC** = the probability that a random positive outscores a random negative (0.5 = coin flip).

**The bar to clear:** `fit_heldout` — the out-of-fold TF-IDF fit model on the same rows. A coverage variant is only worth keeping if coverage or the blend beats it. Changes are made one at a time so each result is attributable.

**Fixed across runs unless stated:** 1,117 positives (812 with career-site text) · 439 hard negatives (audit 7 · fit_top 200 · judged_wrong 299; the judged_wrong slice has fit 0.17–0.72, median 0.23, so it is mostly EASY) · 300 pseudo-negatives · 2,236 postings with requirement units · splitter unchanged · one process, 4 torch threads, native ext4 venv (i7-11370H, WSL2). Every experiment after run 2 runs on a scratch copy of the DB; the live DB keeps the production evidence.

## Summary

| Run | Change | coverage_required | coverage_gated | fit_heldout | best blend | sanity (vs pseudo) | medians pos / hard |
|---|---|---|---|---|---|---|---|
| R1 | baseline, evidence embedded 9/15 (missing 30 units) | 0.562 | 0.558 | 0.690 | 0.712 (w 0.6) | 0.806 | 46.0 / 44.9 |
| R2 | baseline, evidence synced from Postgres | 0.563 | 0.559 | 0.706 | 0.706 (w 0.75) | 0.806 | 46.2 / 45.0 |
| S1 | evidence = bullets + duty statements + SOAR only | 0.550 | 0.543 | 0.706 | 0.661 (w 0.6) | 0.785 | 23.2 / 20.2 |

## R1 — baseline (2026-09-16 15:48, calibration `daaf23f5d142`, 1,138 s)
First run that completed (earlier attempts died on the 9p mount; see STATUS). Evidence: 2,888 units embedded 2026-09-15 23:39, before 30 units of bullet edits on 9/16.
- Thresholds: COVER_STRONG 0.74 · COVER_PARTIAL 0.62 · COVERAGE_REJECT 34.979 · COVERAGE_REVIEW 39.834
- AUC vs hard negatives: coverage_required 0.562 · coverage_role 0.523 · coverage_gated 0.558 · fit_heldout 0.690 · blend 0.5/0.6/0.75/0.9 = 0.706/0.712/0.711/0.656
- Paired gap (vault copy minus career-site copy, n=40): 1.723 points — the JD's source barely moves coverage.
- Top hard negatives by coverage are engineering roles written in data/AI vocabulary: Guidehouse Data Platform Lead (67.8), AHEAD ServiceNow Principal Technical Consultant - AI (63.4), Guidehouse Palantir Enterprise Ontology Architect (58.3), Machinify Staff AI Engineer (57.5), Blue Yonder Principal Data Platform Engineer (56.3), Forward Financing Senior Software Engineer MLOps (55.8).
- **Reading:** coverage separates obvious mismatches (0.81 vs random postings) but not close calls (0.56). It measures topic overlap, not the kind of work.

## R2 — baseline with current evidence (16:12, calibration `0d51cea53c9b`, 1,178 s)
Same as R1 after `evidence --rebuild` from PostgreSQL (`resume.vw_resume_bullets` etc.): 2,918 units, 30 newly embedded. First run from `~/jobsearch`.
- Thresholds: 0.74 / 0.62 · REJECT 34.979 · REVIEW 39.932
- AUC: coverage_required 0.563 · coverage_role 0.524 · coverage_gated 0.559 · fit_heldout 0.706 · blend 0.700/0.705/0.706/0.656
- Paired gap 1.706 (n=40).
- **Reading:** confirms R1. The blend now equals the fit model alone: coverage adds nothing to ranking. This is the baseline for every step below.

## S1 — core evidence only (16:37, calibration `10abbbb80dbb`, 593 s; scratch DB)
Hypothesis: 1,803 article units dominate the evidence and make data/AI engineering roles look covered. Manifest restricted to `resume_bullets`, `duty_statements`, `soar_stories` (415 units; articles, profile site, bio, LinkedIn and signature method removed). Requirement units reused from cache, hence the shorter run.
- Thresholds: 0.76 / 0.64 · REJECT 4.768 · REVIEW 11.321
- AUC: coverage_required 0.550 · coverage_role 0.507 · coverage_gated 0.543 · fit_heldout 0.706 · blend 0.652/0.661/0.648/0.590 · sanity 0.785
- Top hard negatives are still engineering/AI roles: Machinify Staff AI Engineer (50.0), Guidehouse Palantir Enterprise Ontology Architect (50.0), Novartis Executive Director Agentic Lab (45.0), Blue Yonder Principal Data Platform Engineer (43.8), Marriott Director AI Application Engineering (42.6).
- **Reading:** rejected. Removing articles halved everyone's coverage, positives and negatives alike, and the ranking got slightly worse. The evidence mix is not the cause; sentence-level topic matching is. **Production keeps the full evidence manifest.**

## Plan for the next steps (decided with the user 2026-09-16)
- **S2** — requirement text embedded with the posting title around it (`"<title>: <requirement>"`), bge-small. Also adds a second negative set: postings the judge graded `stretch` (the confusable band), with a re-run baseline (S2a) so the stretch AUC has a reference.
- **S3** — bge-base-en-v1.5 (768-d, ~440 MB) in place of bge-small (384-d).
- **S4** — a cross-encoder reranker over each requirement's top evidence matches (cross-encoder/ms-marco-MiniLM-L-6-v2 first; BAAI/bge-reranker-base if time allows).
- Stop rule: a step is kept only if it beats `fit_heldout` on hard negatives or clearly improves the stretch AUC. bge-large / Qwen3-Embedding are out of scope unless S4 shows model capacity is the bottleneck.
