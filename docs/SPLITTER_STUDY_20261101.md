# Splitter study, 2026-11-01 (splitter 2026-10-30.1)

Why: the 11/01 audit found Jev reading duties, preferred certifications and pay text as Required lines (GDIT
`d3559ae9ac4a32f7111c`, Deloitte `2839e9ccd92d3d453c77`, ServiceNow `4c0d5079f753d7041c65`, McKesson
`7816d2189dd42c83b95a`). This study measures how job descriptions label their sections across the corpus and where the
current splitter misfiles them. Read-only; nothing in `backend/` changed.

## Method
`scripts/splitter_study/collect.py` runs the current splitter on two samples and writes one JSON line per posting:
every heading (recognized, or heading-shaped but unrecognized = `cand`), a profile of the up-to-12 lines under it
(qualification-like `q`, verb-led duty `d`, preferred cue `p`, judge noise `z`, certification `c`), and the
Required / Responsibility / Preferred line counts the judges see (`judge2.section_lines`).

- **Population**: the 1,745 active, unrejected postings judges and Jev read (`--population`).
- **Corpus**: up to 60 active fit-track postings per employer, 7,916 postings across 174 employers.
- 9,315 unique postings in all. Analysis: `rates.py`, `headings.py`, `flips.py`, `line_status.py`, `openers.py`
  (each takes the JSONL files); `show.py <posting_id>...` prints a JD with each line's heading tag and section.
- The q/d/p profiles are regex heuristics: good for ranking patterns, not exact counts. Every fix below was checked
  against real postings with `show.py`.

## Baseline (population / corpus)
| Measure | Population | Corpus |
|---|---|---|
| Required lines > 30 (Jev cap) | 3.1% | 3.1% |
| 0 Required lines | 4.2% | 4.2% |
| Required lines but 0 Responsibility lines | 8.5% | 15.7% |
| A Required line carries a preferred cue | 14.6% | 16.5% |
| Required lines led by "Preferred/Desired/Bonus" | 1.7% | 2.2% |
| Median / p90 Required lines | 9 / 21 | 10 / 21 |

Oracle ORC: 56 of 360 population postings (15.6%) have 0 Required lines. **Not a splitter defect**: Oracle's own
`ExternalQualificationsStr` holds only the disclaimer and benefits (checked req 343265 against the live API). The
posting states no qualifications; judges should read that as "none stated".

## Findings, by impact on what Jev reads

### 1. Section flips back to Required (largest)
- **After Preferred: 1,003 postings.** Any heading matching `PERSON_HEADINGS` re-opens Required, including:
  - a content line that merely looks like a heading: Deloitte's first Preferred item "Azure DevOps experience" (short
    label + the word "experience") flipped its whole Preferred list, plus the pay text after it, into Required;
  - soft trait lists: "Competencies", "Core Competencies", "Knowledge, Skills and Abilities", "Professional Skills"
    (Cooley, QTS, Included Health, ICF, CBTS, Voya);
  - non-requirements: "Educational Assistance/Tuition Reimbursement" (BD), "Physical Demand Requirements" (USAA; the
    drop regex only knows "physical demands|requirements"), "FINRA Requirements" ("FINRA licenses are not required"),
    xAI "Additional Requirements" (full/part-time logistics);
  - "While not required, we would be especially excited if you bring" (Carrot).
- **Inside duties: 108 postings.** A topic subheading with a person word followed by verb-led duties:
  "Content and knowledge amplification" (ServiceNow), "Communication Skills" (CVS), "Business Knowledge" (T. Rowe),
  and "requirements" as a work object: "Requirements Gathering", "Business Analysis & Requirements Management",
  "Technical Requirements & Solution Framing" (GitLab, UKG, MetLife, Marriott).

Fix: after a Preferred heading, only an explicit gate heading (required / requirements / minimum / basic / must /
what you need) re-opens Required; topic headings inherit. A short line with no colon is a heading only when the
heading phrase is the whole line (the `_whole_heading` test), never because it contains "experience" or "skills".
"requirements" followed by gathering / management / translation / definition / discovery / framing is not a heading cue.
A topic heading whose next lines are mostly verb-led duties stays in Responsibilities.

### 2. Headings that open or keep the wrong section
| Pattern | Postings | Examples | Fix |
|---|---|---|---|
| Team, company, benefit and tag headings not dropped; their lines stay in Required (977) or Preferred (647) | 2,670 | Company Overview, The team, What We Believe, Work Shift, Parental Leave, Employee Referral Program, Paid Time Off, Sponsorship, LI-Remote / LI-Hybrid tags, "259000 291000 USD" | add to `DROP_HEADINGS` (tag and salary lines as drop lines) |
| "bonus" in `PREFERRED_HEADINGS` opens Preferred on benefit headings | 221 | Peer Bonus Awards (76), Annual Bonus Eligibility (60), Bonus Potential | benefit-shaped bonus headings drop; keep "Bonus points", "Bonus if", bare "Bonus" |
| "Job Description:" / "Overview" after a metadata block leaves the whole JD in Required | 106 | GDIT: "Public Trust/Other Required: None" ... "Job Description:" | these headings reset to intro; "Field: None" metadata lines drop |
| Duty openers not recognized | 179 | Your Mission, The Work, Essential Job Functions, Job Functions, What you'll work on, How you'll make a difference, The successful candidate will, Work you'll do (Deloitte), "...will execute the following responsibilities:" (GDIT; > 80 chars so not a heading) | add to `RESPONSIBILITY_HEADINGS`; a colon line naming "the following responsibilities/duties" is a heading up to 150 chars |
| Required openers not recognized | 198 | Minimum Requirement (singular; McKesson), Qualification, License/Certifications, Licensing, Languages | add; mostly harmless today (they already sit in Required) but 19 sit under duties |
| Preferred openers not recognized | 13 | What will make you stand out, Even better, Differentiators, Ways to stand out | add |

### 3. Line-level status inside Required (1,486 postings, 1,747 lines)
| Pattern | Lines | Example | Fix |
|---|---|---|---|
| Leading label | ~220 | "Desired experience with C, C++...", "Good to have: ..." | `inline_section`: a leading Preferred / Desired / Nice to have / Good to have / Bonus / Advantageous cue sets Preferred |
| Trailing status not in the list | 210 | "(desirable)", "(advantageous)", "is an asset" | widen `INLINE_STATUS` |
| Status mid-line | ~270 | "Hospitality experience is a plus, in particular...", "JD degree desirable, but not required", "Bachelor's degree not required, but recommended" | "is a plus / desirable / not required" on the line's own subject (outside parentheses) sets Preferred |
| Two statuses in one line | 181 | "Bachelor's required; Master's preferred" | split into clauses before classifying |
| Soft qualifier on a real requirement | ~600 | "5+ years ..., ideally with Open Source", "cloud platforms (AWS preferred)" | leave Required: the gate is real, the qualifier is soft (Jev's grey question covers it) |

### 4. "One of the following" lists: 336 postings
Deloitte's "One of the following active accreditations obtained:" lists 22 certifications as 22 Required lines;
any one satisfies the gate. Fix: collapse an any-of list (one of / any of / at least one of the following) into
a single Required line, "One of: CPA; licensed attorney; Enrolled Agent; ...".

### 5. Portrait before the formal block (lower confidence): 1,010 postings
McKesson: a narrative "What You Bring" and "Technical Skills" come before the formal "Minimum Requirement /
Education / Critical Skills" block, so 60 Required lines restate each other. The same shape covers real cases, though
("Education and Experience" before "Qualifications"), so the rule needs measuring first: when a formal gate heading
follows, earlier non-gate Required sections act like the soft "persona" block (qualification-shaped lines stay
Required, the rest become Preferred).

### 6. Sentence-case subheadings counted as lines
ServiceNow's "Community strategy and governance", "Measurement and continuous improvement" are labels in sentence
case, so `_subheading` (Title Case only) keeps them as content lines. Fix (small): a <= 6-word, verb-free,
unpunctuated line followed by a longer line is a label.

## Cost of shipping
Any splitter change re-keys Jev's prompt_version, so the next run re-scores the 133-posting gold set (~5.5M tokens,
11/01) and re-splits coverage. Ship every fix above as ONE `SPLITTER_VERSION`, re-run `collect.py` on both samples
and compare against this baseline before merging.

## Result: splitter 2026-11-01.1 (branch `splitter-20261101`)
Measured with the same two samples (`collect.py`, then `rates.py`), baseline 2026-10-30.1 vs 2026-11-01.1:

| Measure | Population before / after | Corpus before / after |
|---|---|---|
| Required lines > 30 (Jev cap) | 3.1% / 1.4% | 3.1% / 2.1% |
| A Required line carries a preferred cue | 14.6% / 8.7% | 16.5% / 9.7% |
| Required lines led by "Preferred/Desired/Bonus" | 1.7% / 0.1% | 2.2% / 0.2% |
| Required section mostly duties | 1.7% / 0.6% | 1.0% / 0.6% |
| Required lines but 0 Responsibility lines | 8.5% / 6.6% | 15.7% / 13.6% |
| Median / p90 Required lines | 9 / 21 -> 8 / 18 | 10 / 21 -> 8 / 19 |
| Total Required lines | 18,599 -> ~16,400 | 88,116 -> ~79,500 |

Required lines changed in about 26% of population postings. 6 population postings lost every Required line, all
correctly: Oracle "preferred but not required" lines and duties ending "as required". The corpus's 50 such losses
(Toast "AI at Toast" marketing, McKesson physical demands, "a plus but not required") were checked the same way.
The 4 audit cases: GDIT Required 41 -> 14 with 20 duties found (was 0); Deloitte 40 -> 10 with the 22 accreditations
as one "One of ..." line and the soft skills moved to Preferred; ServiceNow 44 -> 32 with its duties back in
Responsibilities; McKesson 60 -> 59 (see §5).

Note: `flips.py` counts raw heading tags (`_heading_kind`), before `_resolve_kinds`, so its numbers (1,003 -> 823)
understate the change; the line-level measures above are what Jev reads.

**§5 rejected.** The portrait-before-formal-block rule broke the 2026-10-20 measured decision that a hard-worded
heading ("Top skills or competencies to be successful") carries real requirements even when a "Basic
Qualifications" block follows (`tests/test_splitter_persona.py`). It was removed; McKesson and ServiceNow stay long,
and Jev's over-cap rule keeps qualification-shaped lines first.

**Jev cost of shipping it.** With the 11/01 carry-over (`jev.request_base`, jev `9e3c22d`), a review is reused at 0
tokens when the new splitter selects the same lines. Of the 237 reviews under prompt_version d85211a8b443: gold
set 74 of 133 carry over (5.56M -> ~2.74M tokens to re-measure), other reviews 76 of 104 carry over (4.40M ->
~1.31M). The gold set has grown to 143 since; the 10 new ones are reviewed either way.
