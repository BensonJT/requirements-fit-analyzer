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
| S2a | R2 re-run, adds the stretch set (806) | 0.563 | 0.559 | 0.706 | 0.706 (w 0.75) | 0.806 | 46.2 / 45.0 |
| **S2** | **requirement embedded as "<title>: <requirement>"** | **0.610** | **0.607** | 0.706 | 0.736 (w 0.6) | 0.822 | — |
| S4a | MiniLM cross-encoder reranks top-5 evidence per requirement (bge-small, no title) | 0.605 | 0.604 | 0.706 | 0.651 (w 0.9) | **0.838** | — |
| S4b | S4a + title context (S2) | **0.617** | **0.617** | 0.706 | 0.692 (w 0.75) | 0.808 | — |

**Against `stretch` (the confusable band, never used to pick thresholds):**

| Run | coverage_gated | fit_heldout | blend (best w) |
|---|---|---|---|
| S2a | 0.575 | **0.955** | 0.854 |
| S2 | 0.610 | 0.955 | 0.875 |
| **S4a** | **0.669** | 0.955 | 0.757 |
| S4b | 0.664 | 0.955 | 0.808 |

**Against judge-confirmed `wrong` only (n=299, reported from S2 on):** S2 coverage 0.637 · S4a coverage 0.677 · **S4b coverage 0.683** · fit_heldout 0.995 in all.

Note on `fit_heldout` for hard negatives: up to S2 the code read the live screen's fit_prob, which is in-sample for training rows; from S3/S4a it uses out-of-fold scores (1,317 of the 1,320 judged-wrong postings are training rows). The judged-wrong figure stayed at 0.995 after the fix, so these rows are genuinely easy for the fit model, not leaked.

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

## S2a — baseline plus a stretch negative set (16:49, calibration `49e4f039319b`, 673 s; scratch DB)
Code change only in reporting: calibration now also scores the 806 postings the judge graded `stretch` (label 0, career-site text ≥ 800 chars) and reports AUC against them. They are not used to choose thresholds.
- Hard-negative AUCs identical to R2 (as they must be): coverage_gated 0.559 · fit_heldout 0.706 · blend_0.75 0.706.
- **vs stretch:** coverage_required 0.576 · coverage_gated 0.575 · **fit_heldout 0.955** · blend_0.75 0.854.
- **Reading — the yardstick was biased.** The fit model separates positives from the confusable band almost perfectly on held-out folds, yet looked mediocre (0.706) against the hard negatives. 200 of those 439 are `fit_top`: unlabeled postings selected *because* the fit model scored them highest, which caps the fit model's AUC on that set by construction (and they are unconfirmed guesses, not judged negatives). Coverage is weak on both sets, and blending it in drags the stretch AUC from 0.955 to 0.854. From S2 on, calibration also reports AUC against the 299 judge-confirmed `wrong` rows alone.

## S2 — title context around each requirement (17:01, calibration `4b54b8aa215d`, 3,315 s; scratch DB)
`JOBSEARCH_REQ_CONTEXT=title`: each requirement unit is embedded as `"<posting title>: <requirement>"`; evidence units unchanged; bge-small. Nearly every unit becomes unique per posting (~7,000 distinct units per 200 postings), so embedding took ~48 min against ~10 for plain units.
- Thresholds: COVER_STRONG 0.70 · COVER_PARTIAL 0.62 · REJECT 28.271 · REVIEW 37.857
- AUC vs hard negatives: coverage_required **0.610** · coverage_role 0.584 · coverage_gated **0.607** · fit_heldout 0.706 · blend 0.730/**0.736**/0.720/0.663 · sanity 0.822
- AUC vs judge-confirmed wrong only (n=299): coverage_gated 0.637 · fit 0.995 · blend_0.6 0.948. The fit figure here came from the live screen (in-sample for these rows); `hard_fit` now uses out-of-fold scores, and S4a showed the same 0.995 with them, so the number holds.
- AUC vs stretch (n=806): coverage_required 0.605 · coverage_gated **0.610** · fit_heldout 0.955 · blend_0.6 0.875
- Top hard negatives by coverage: Machinify AI Engineer Agentic Systems (82.2), Salesforce Senior Manager Global Cloud Campaigns (80.0), Machinify Staff AI Engineer (80.0), CVS Health Senior Manager Release Engineering (80.0), Amgen Senior Manager Global ERP Operations Lead SAP (78.3), Databricks Director Agent & AI Search (77.3), Navy Federal Principal AI Engineer Agentic AI (76.9).
- **Reading — the first real gain.** +0.048 on hard negatives and +0.035 on stretch, on coverage alone, from one line of context. It confirms the diagnosis (bare requirement sentences lose the role) but coverage is still far below the fit model on the confusable band (0.61 vs 0.955), and blending still lowers the stretch AUC. Kept as a candidate for S4; not a ranking signal on its own. Cost: ~5× the embedding time.

## S4a — cross-encoder reranker, plain requirements (18:28, calibration `cafe29793f2a`, 8,238 s; scratch DB)
`JOBSEARCH_RERANKER=cross-encoder/ms-marco-MiniLM-L-6-v2`, `JOBSEARCH_RERANK_TOP=5`, bge-small retrieval, no title context. For each requirement the 5 cosine-nearest evidence units are read together with the requirement by the cross-encoder; its sigmoid probability replaces the cosine in the band rule, on a 0.30–0.95 / 0.05–… grid. 332,686 distinct pairs in 8,085 s (~41 pairs/s at 4 threads).
- Smoke test before the run: "Process Excellence: Lead Lean Six Sigma projects" vs an LSS bullet 0.603; "Staff AI Engineer: build ML pipelines with MLOps" vs an article about AI assistants **0.000** — the distinction cosine misses.
- Thresholds: COVER_STRONG **0.95** · COVER_PARTIAL **0.05** — both at the edge of the grid. Almost nothing is "strong"; any probability ≥ 0.05 earns partial credit. The optimum wants a *continuous* credit, not bands (candidate S5).
- AUC vs hard negatives: coverage_required 0.605 · coverage_role 0.547 · coverage_gated 0.604 · fit_heldout 0.706 · blend 0.597/0.616/0.647/0.651 · sanity **0.838** (best so far)
- AUC vs judge-confirmed wrong (n=299): coverage_gated **0.677** · fit 0.995 · blend_0.9 0.805
- AUC vs stretch (n=806): coverage_required **0.670** · coverage_gated **0.669** · fit 0.955 · blend_0.9 0.757
- **Top hard negatives changed character.** The data/AI engineering roles are gone from the top. The highest-coverage "negatives" are now unlabeled `fit_top` rows that read like real fits: Capital One Manager Project Management – Product Operations (43.4), Microsoft Senior Manager Sales Operations Strategy Enablement (39.4), Navy Federal Program Manager (33.3), Microsoft Senior Technical Program Manager (31.4), **McKesson Lead Workforce Intelligence Consultant (29.7)**, Oracle Principal Program Manager – Repair Services Operations (27.8). Only two judged-`wrong` rows in the top 10.
- **Reading — the reranker fixes the failure mode S1 diagnosed.** Best coverage result on the confusable band (+0.094 over baseline vs stretch, +0.04 vs judged wrong), best sanity AUC, and the false positives are now plausible fits rather than engineering jobs. The hard-negative AUC (0.604) is held down by `fit_top`, which increasingly looks like it contains real positives — that set should not be the calibration target. Coverage is still far below the fit model on ranking (0.67 vs 0.955) and blending still hurts; its value is as an **explainer** whose matches are now trustworthy, not as a ranker. Cost: ~2 h 15 min of CPU for the calibration set; the daily increment would be a few hundred postings.

## S4b — reranker plus title context (20:46, calibration `4ae63f653f8c`, 10,633 s; scratch DB)
S4a's reranker (MiniLM, top 5) on S2's title-context retrieval; the cross-encoder also reads `"<title>: <requirement>"`. 408,972 distinct pairs in 10,599 s (~39 pairs/s; fewer cache hits than S4a because titled requirements rarely repeat).
- Thresholds: COVER_STRONG 0.35 · COVER_PARTIAL **0.05** (edge again; the grid is flat from 0.30 to 0.55 for strong) · REJECT 0.000 · REVIEW 0.000
- AUC vs hard negatives: coverage_required 0.617 · coverage_role 0.592 · coverage_gated **0.617** · fit_heldout 0.706 · blend 0.668/0.679/**0.692**/0.677 · sanity 0.808
- AUC vs judge-confirmed wrong (n=299): coverage_gated **0.683** · fit 0.995 · blend_0.75 0.864
- AUC vs stretch (n=806): coverage_required 0.657 · coverage_gated 0.664 · fit 0.955 · blend_0.75 0.808
- Top hard negatives: Amgen Agentic AI Business Solutions Director (80.0), Microsoft Senior Technical Program Manager (80.0), Amgen Senior Manager Global ERP Operations Lead SAP (77.7), Microsoft Senior Repair Network Operations Program Manager (75.0), Salesforce Senior Manager Global Cloud Campaigns (71.1), Navy Federal Principal AI Engineer Agentic AI (62.6), Freddie Mac Engineering Tech Lead Network Observability (57.1).
- **Reading — title context adds nothing on top of the reranker.** Against S4a: +0.013 vs hard negatives and +0.006 vs judged wrong, but −0.005 vs stretch and a lower sanity AUC (0.808 vs 0.838). The differences are within noise, and some title-driven false positives came back (Navy Federal *Principal AI Engineer*, Freddie Mac *Engineering Tech Lead*): the cross-encoder partly matches on the title words instead of the requirement. It costs ~30% more reranker time and ~5× the embedding time. **S4a (reranker, no title) is the better configuration.** Blends are higher here, but every blend still sits well below the fit model alone on stretch (0.808 vs 0.955).

## Plan for the next steps (decided with the user 2026-09-16)
- **S2** — requirement text embedded with the posting title around it (`"<title>: <requirement>"`), bge-small. Also adds a second negative set: postings the judge graded `stretch` (the confusable band), with a re-run baseline (S2a) so the stretch AUC has a reference.
- **S3** — bge-base-en-v1.5 (768-d, ~440 MB) in place of bge-small (384-d).
- **S4** — a cross-encoder reranker over each requirement's top evidence matches (cross-encoder/ms-marco-MiniLM-L-6-v2 first; BAAI/bge-reranker-base if time allows).
- Stop rule: a step is kept only if it beats `fit_heldout` on hard negatives or clearly improves the stretch AUC. bge-large / Qwen3-Embedding are out of scope unless S4 shows model capacity is the bottleneck.
