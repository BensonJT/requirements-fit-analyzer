"""Report-layer tests for the level rule's report ordering (sprint plan sec 20.2) and the applied-AI lens
list (sec 21) -- Agent D, wave 2. Deliberately inserts `screens` / `llm_labels` rows directly (like
test_refresh_hard_negatives_excludes_judge_stretch_grade in test_finder.py) rather than driving the rule
engine, because this file tests report.py's ordering and section-routing logic, not the rules themselves
(Agent A owns rules.py and tests/test_level_fit.py).

Every test runs against the neutral example profile, per the project convention.
"""
import os
import re
import runpy
import sys
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend import profile as P  # noqa: E402
from backend.ats import normalize as N  # noqa: E402
from backend.ats import store  # noqa: E402
from backend.finder import pipeline, report  # noqa: E402

EXAMPLE = {k: v for k, v in runpy.run_path(os.path.join(ROOT, "backend", "profile_local.example.py")).items()
           if k.isupper()}


@pytest.fixture(autouse=True)
def neutral_profile(monkeypatch):
    for key, value in EXAMPLE.items():
        monkeypatch.setattr(P, key, value)


def _quiet(*_a, **_k):
    pass


NOW = datetime(2026, 9, 17, 12, 0)

# One posting per level_fit bucket. Scores are set so that a naive final_score-only sort would rank them
# unknown > stretch_up > in_range -- the OPPOSITE of the required order -- so a passing test proves the
# level CASE is the primary sort key, not an accident of the score column.
LEVEL_ROWS = [
    # req_id,  title,                                  band,     score, verdict,    level_fit
    ("IN1", "Manager, Operational Excellence", "strong", 60, "candidate", "in_range"),
    ("ST1", "Director, Operational Excellence", "strong", 90, "candidate", "stretch_up"),
    ("UN1", "Operations Program Lead", "partial", 95, "candidate", "unknown"),
    ("OR1", "Senior Director, Operational Excellence", "very_strong", 99, "candidate", "out_of_reach"),
    ("TL1", "Operational Excellence Analyst I", "partial", 55, "candidate", "too_low"),
]


def _level_fixture(tmp_path, rows=LEVEL_ROWS):
    con = store.connect(str(tmp_path / "t.duckdb"))
    jobs = [N.base(req_id=req_id, title=title, url=f"https://x/{req_id}", workplace_type="remote",
                   description_text=f"## About the role\nDuties for {title}.\n") for req_id, title, *_ in rows]
    store.record_board(con, "Acme", "greenhouse", jobs, NOW)
    ids = dict(con.execute("SELECT req_id, posting_id FROM postings").fetchall())
    con.executemany(
        "INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict, tier, "
        "rule_score, final_score, band, level_fit, reasons, flags, top_terms) VALUES "
        "(?, 'v1', 'none', ?, ?, 1, ?, ?, ?, ?, '[]', '[]', '[]')",
        [[ids[req_id], NOW, verdict, score, score, band, level_fit]
         for req_id, _title, band, score, verdict, level_fit in rows])
    return con, ids


def _decode_rows(text: str, heading: str, next_heading: str = None) -> list:
    """Pipe-table rows (as plain lists of cells) between `heading` and the next '#'/heading marker."""
    start = text.index(heading) + len(heading)
    chunk = text[start:]
    if next_heading:
        chunk = chunk[: chunk.index(next_heading)]
    out = []
    for line in chunk.splitlines():
        s = line.strip()
        if not s.startswith("|") or set(s) <= set("|- :"):
            continue
        out.append([c.strip() for c in s.strip("|").split("|")])
    return out


def test_write_jobs_found_orders_by_level_and_never_blocks_out_of_range(tmp_path):
    con, ids = _level_fixture(tmp_path)
    meta = {"since": None}
    path = report.write_jobs_found(con, str(tmp_path / "vault"), meta, block_min_band="partial")
    text = path.read_text()

    # Escalated blocks: exactly the three in-range/stretch/unknown postings, in that order -- never
    # out_of_reach or too_low, and never merely sorted by final_score (which would put UN1 first).
    block_titles = re.findall(r"## Title: (.+)", text)
    assert block_titles == ["Manager, Operational Excellence", "Director, Operational Excellence",
                            "Operations Program Lead"]
    assert text.count("\n# Company:") == 3

    # Summary table: same three, same order, plus a Level column; out_of_reach/too_low absent.
    header, *rows = _decode_rows(text, report.SUMMARY_HEADING, "## Escalated Roles")
    assert [c.lower() for c in header] == ["posting id", "company", "title", "score", "band", "tier",
                                           "level", "decision", "reason"]
    level_col = [c.lower() for c in header].index("level")
    row_pids = [r[0] for r in rows]
    assert row_pids == [ids["IN1"], ids["ST1"], ids["UN1"]]
    assert [r[level_col] for r in rows] == ["in_range", "stretch_up", "unknown"]
    assert ids["OR1"] not in row_pids and ids["TL1"] not in row_pids

    # Collapsed tail: out_of_reach and too_low, score descending, with a Level column and never a block.
    assert "<details><summary>Out of level range (2)</summary>" in text
    tail_header, *tail_rows = _decode_rows(text, "<summary>Out of level range (2)</summary>", "</details>")
    assert [c.lower() for c in tail_header] == ["posting id", "company", "title", "score", "band", "tier",
                                                "level", "decision", "reason"]
    tail_level_col = [c.lower() for c in tail_header].index("level")
    assert [r[0] for r in tail_rows] == [ids["OR1"], ids["TL1"]]                 # 99 before 55: score desc
    assert [r[tail_level_col] for r in tail_rows] == ["out_of_reach", "too_low"]


def test_write_jobs_found_reparses_and_read_back_unaffected_by_collapsed_tail(tmp_path):
    con, ids = _level_fixture(tmp_path)
    vault = tmp_path / "vault"
    path = report.write_jobs_found(con, str(vault), {"since": None}, block_min_band="partial")
    # No decisions marked yet.
    assert report.parse_decisions(path) == []
    text = path.read_text()
    out = []
    for line in text.splitlines():
        if line.startswith(f"| {ids['IN1']} |"):
            line = line[: line.rindex("|  |  |")] + "| build |  |"
        out.append(line)
    path.write_text("\n".join(out) + "\n")
    decisions = report.parse_decisions(path)
    assert [d.posting_id for d in decisions] == [ids["IN1"]]
    assert report.read_back(con, str(vault)) == 1
    assert report.read_back(con, str(vault)) == 0


def test_write_lens_lists_level_column_and_applied_ai_bucket(tmp_path):
    con, ids = _level_fixture(tmp_path)
    now = NOW
    # A row strong on AI and on process (appears in both the AI list and the process list), and a row
    # strong on AI only.
    ai_both_job = N.base(req_id="AIBOTH", title="Applied AI Program Manager", url="https://x/AIBOTH",
                         workplace_type="remote", description_text="Own agentic delivery and enablement.")
    ai_only_job = N.base(req_id="AIONLY", title="AI Enablement Lead", url="https://x/AIONLY",
                         workplace_type="remote", description_text="Own agentic delivery and enablement.")
    store.record_board(con, "Acme", "greenhouse", [ai_both_job, ai_only_job], now)
    extra = dict(con.execute("SELECT req_id, posting_id FROM postings WHERE req_id IN ('AIBOTH', 'AIONLY')")
                .fetchall())
    con.executemany(
        "INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict, tier, "
        "rule_score, final_score, band, level_fit, reasons, flags, top_terms) VALUES "
        "(?, 'v1', 'none', ?, 'candidate', 1, ?, ?, 'strong', ?, '[]', '[]', '[]')",
        [[extra["AIBOTH"], now, 80, 80, "stretch_up"], [extra["AIONLY"], now, 75, 75, "unknown"]])
    hashes = dict(con.execute("SELECT posting_id, description_hash FROM postings").fetchall())
    con.executemany(
        "INSERT INTO llm_labels (posting_id, description_hash, rubric_version, scorer, grade, grade_process, "
        "grade_technical, grade_ai, lane, confidence, blocker, rationale, batch, judged_at) VALUES "
        "(?, ?, 'rv1', 'test', 'bullseye', ?, NULL, ?, 'primary', 'high', '', '', 'b', ?)",
        [[extra["AIBOTH"], hashes[extra["AIBOTH"]], "bullseye", "bullseye", now],
         [extra["AIONLY"], hashes[extra["AIONLY"]], None, "adjacent", now]])

    path = report.write_lens_lists(con, str(tmp_path / "vault"))
    text = path.read_text()

    assert f"## {report.AI_LIST[1]}" in text
    ai_header, *ai_rows = _decode_rows(text, f"## {report.AI_LIST[1]}", None)
    assert [c.lower() for c in ai_header] == ["posting id", "rank", "company", "title", "why", "score", "band",
                                              "level", "ai", "required", "embed", "bull", "j2", "placed by", "pay",
                                              "location", "age"]
    ai_pids = [r[0] for r in ai_rows]
    assert extra["AIBOTH"] in ai_pids and extra["AIONLY"] in ai_pids
    # Neither the level rule's in_range/out_of_reach postings nor `neither`-bucket rows leak into this list.
    assert ids["IN1"] not in ai_pids

    # The row strong on AI and process appears in BOTH the process list and the AI list.
    process_header, *process_rows = _decode_rows(text, "## Strong on PROCESS", "## Strong on TECHNICAL")
    assert extra["AIBOTH"] in [r[0] for r in process_rows]
    assert "level" in [c.lower() for c in process_header]


def test_top_location_cell_leads_with_the_long_commute_site():
    """2026-09-22: the screen's long-commute flag was invisible in the top list; it now leads the Location cell."""
    from backend.finder import report as R
    m = R._LONG_COMMUTE_RE.search("x; local/hybrid via Peoria, IL -- long commute: check the exact site and in-office days")
    assert m and m.group(1) == "Peoria, IL"
    assert R._loc_cell("Springfield, IL", None) == "Springfield, IL"
    assert R._loc_cell("Chicago, IL", "Peoria, IL").startswith("⚠ long commute: Peoria, IL")
