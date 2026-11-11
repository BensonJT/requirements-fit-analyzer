"""Unit tests for the ATS ingestion layer — no network, temp DuckDB file."""
import json
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.ats import normalize as N  # noqa: E402
from backend.ats import store  # noqa: E402


# ---------------------------------------------------------------- normalize
def test_parse_date_shapes():
    today = date(2026, 9, 14)
    assert N.parse_date("Posted Today", today) == today
    assert N.parse_date("Posted Yesterday", today) == date(2026, 9, 13)
    assert N.parse_date("Posted 3 Days Ago", today) == date(2026, 9, 11)
    assert N.parse_date("Posted 30+ Days Ago", today) is None
    assert N.parse_date("2026-09-02T01:19:40.213+00:00") == date(2026, 9, 2)
    assert N.parse_date("2026-09-10") == date(2026, 9, 10)
    assert N.parse_date(1784642408166) == date(2026, 7, 21)
    assert N.parse_date(None) is None


def test_workplace_type():
    assert N.workplace_type("ORA_HYBRID") == "hybrid"
    assert N.workplace_type("Remote") == "remote"
    assert N.workplace_type("onsite") == "onsite"
    assert N.workplace_type(True) == "remote"
    assert N.workplace_type(None, "Remote - USA") == "remote"
    assert N.workplace_type(None, "Vienna, VA") is None


def test_primary_location_drops_workday_collapse():
    assert N.primary_location("7 Locations") is None
    assert N.primary_location("Remote - USA") == "Remote - USA"


def test_pay_from_text():
    assert N.pay_from_text("The salary range is $85,000 - $105,000 annually.") == (85000, 105000, "year")
    assert N.pay_from_text("pays $19.37 - $22.76 per hour") == (19, 22, "hour")
    assert N.pay_from_text("range: $110K to $135K") == (110000, 135000, "year")
    assert N.pay_from_text("no money here") is None


def test_html_to_text_handles_double_escaped():
    assert N.html_to_text("&lt;p&gt;Hello &amp;amp; bye&lt;/p&gt;") == "Hello & bye"
    assert N.html_to_text("<ul><li>a</li><li>b</li></ul>") == "a\nb"


def test_html_to_text_keeps_inline_tags_from_splitting_a_sentence():
    """An inline tag (<strong>, <em>, <b>, <span>, ...) must not break a sentence onto its own
    line -- get_text("\\n") did this for every tag alike, so 'Lean Six Sigma' with the first
    word bolded came out 'Lean\\n Six Sigma'. Only block-level tags start a new line."""
    assert N.html_to_text("<p><strong>Lean</strong> Six Sigma</p>") == "Lean Six Sigma"
    assert N.html_to_text("Own <b>process excellence</b> for the region.") == "Own process excellence for the region."


def test_html_to_text_still_breaks_lines_on_block_tags():
    assert N.html_to_text("<p>First paragraph.</p><p>Second paragraph.</p>") == "First paragraph.\nSecond paragraph."
    assert N.html_to_text("Line one<br>Line two") == "Line one\nLine two"


def test_html_to_text_drops_script_and_style_content():
    assert N.html_to_text("<p>Visible</p><script>var x = 'not text';</script><style>.a{color:red}</style>") == "Visible"


# ---------------------------------------------------------------- store lifecycle
def _job(req, title="Process Lead", desc=None):
    return N.base(req_id=req, title=title, url=f"https://x/{req}", description_text=desc)


def test_upsert_then_close_then_reopen(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    t1, t2, t3 = datetime(2026, 9, 14, 10), datetime(2026, 9, 15, 10), datetime(2026, 9, 16, 10)

    seen, new, reopened, closed = store.record_board(con, "Acme", "workday", [_job("R1"), _job("R2")], t1)
    assert (seen, new, reopened, closed) == (2, 2, 0, 0)

    # Day 2: R2 gone, R3 new, R1 unchanged -> R1 updated in place (not re-added), R2 closed.
    seen, new, reopened, closed = store.record_board(con, "Acme", "workday", [_job("R1", "Process Lead II"), _job("R3")], t2)
    assert (seen, new, reopened, closed) == (2, 1, 0, 1)
    rows = {r[0]: r for r in con.execute("SELECT req_id, status, first_seen_at, last_seen_at, closed_at, title FROM postings").fetchall()}
    assert rows["R1"][1] == "active" and rows["R1"][2] == t1 and rows["R1"][3] == t2 and rows["R1"][5] == "Process Lead II"
    assert rows["R2"][1] == "closed" and rows["R2"][4] == t2
    assert con.execute("SELECT count(*) FROM postings").fetchone()[0] == 3  # nothing deleted

    # Day 3: R2 reappears -> reopened, closed_at cleared, first_seen_at preserved.
    seen, new, reopened, closed = store.record_board(con, "Acme", "workday", [_job("R1"), _job("R2"), _job("R3")], t3)
    assert (new, reopened, closed) == (0, 1, 0)
    r2 = con.execute("SELECT status, closed_at, first_seen_at FROM postings WHERE req_id = 'R2'").fetchone()
    assert r2 == ("active", None, t1)


def test_truncated_pull_never_closes(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    t1, t2 = datetime(2026, 9, 14), datetime(2026, 9, 15)
    store.record_board(con, "Big", "workday", [_job("R1"), _job("R2")], t1)
    _, _, _, closed = store.record_board(con, "Big", "workday", [_job("R1")], t2, truncated=True)
    assert closed == 0
    assert con.execute("SELECT count(*) FROM postings WHERE status = 'active'").fetchone()[0] == 2


def test_close_moved_board_postings_closes_only_the_old_board(tmp_path):
    """Spring Health moved Greenhouse -> Ashby: its Greenhouse rows must close, its Ashby rows,
    another employer's Greenhouse rows and its own bridge rows must not."""
    con = store.connect(str(tmp_path / "t.duckdb"))
    t1, t2 = datetime(2026, 9, 14), datetime(2026, 10, 22)
    store.record_board(con, "Spring Health", "greenhouse", [_job("G1"), _job("G2")], t1)
    store.record_board(con, "Other", "greenhouse", [_job("O1")], t1)
    store.record_board(con, "Spring Health", "ashby", [_job("A1")], t2)
    moved = store.close_moved_board_postings(con, "Spring Health", "ashby", t2)
    assert moved == {"greenhouse": 2}
    rows = {(r[0], r[1]): r[2] for r in con.execute("SELECT employer, req_id, status FROM postings").fetchall()}
    assert rows == {("Spring Health", "G1"): "closed", ("Spring Health", "G2"): "closed",
                    ("Other", "O1"): "active", ("Spring Health", "A1"): "active"}
    assert store.close_moved_board_postings(con, "Spring Health", "ashby", t2) == {}  # idempotent


def _sweep_with(monkeypatch, con, row, jobs):
    from backend.ats import adapters as A
    from backend.ats import sweep as ats_sweep
    monkeypatch.setattr(A, "list_jobs", lambda r, max_pages=None, scope=None: jobs)
    logged = []
    stats = ats_sweep.sweep(con, [row], workers=1, log=logged.append)
    return stats, logged


def test_sweep_closes_an_employers_old_board_after_a_clean_pull(tmp_path, monkeypatch):
    con = store.connect(str(tmp_path / "t.duckdb"))
    store.record_board(con, "Spring Health", "greenhouse", [_job("G1")], datetime(2026, 9, 14))
    row = {"employer": "Spring Health", "platform": "ashby"}
    stats, logged = _sweep_with(monkeypatch, con, row, [_job("A1")])
    assert con.execute("SELECT status FROM postings WHERE req_id = 'G1'").fetchone()[0] == "closed"
    assert stats["closed"] == 1
    assert any("old greenhouse board" in line for line in logged)


def test_sweep_never_closes_an_old_board_on_a_truncated_or_empty_pull(tmp_path, monkeypatch):
    from backend.ats import adapters as A
    con = store.connect(str(tmp_path / "t.duckdb"))
    store.record_board(con, "Spring Health", "greenhouse", [_job("G1")], datetime(2026, 9, 14))
    row = {"employer": "Spring Health", "platform": "ashby"}
    _sweep_with(monkeypatch, con, row, [])                                   # clean but empty
    _sweep_with(monkeypatch, con, row, A.Truncated([_job("A1")]))            # truncated
    assert con.execute("SELECT status FROM postings WHERE req_id = 'G1'").fetchone()[0] == "active"


def test_description_is_never_blanked_by_a_thin_list_pull(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    t1, t2 = datetime(2026, 9, 14), datetime(2026, 9, 15)
    store.record_board(con, "Acme", "workday", [_job("R1")], t1)
    store.apply_detail(con, store.posting_id("Acme", "workday", "R1"), {"description_text": "full JD"}, t1)
    store.record_board(con, "Acme", "workday", [_job("R1")], t2)  # list pull carries no JD
    assert con.execute("SELECT description_text FROM postings").fetchone()[0] == "full JD"


def test_duplicate_req_in_one_pull_is_collapsed(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    seen, new, _, _ = store.record_board(con, "Acme", "greenhouse", [_job("1"), _job("1")], datetime(2026, 9, 14))
    assert new == 1 and con.execute("SELECT count(*) FROM postings").fetchone()[0] == 1


def test_views_and_macros(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    now = datetime.now()
    jobs = [N.base(req_id="A", title="Director of Operational Excellence", location_primary="Remote - USA",
                   description_text="Lean Six Sigma Black Belt required. $95,000 - $110,000"),
            N.base(req_id="B", title="Pharmacy Technician", location_primary="Tulsa, OK")]
    store.record_board(con, "Acme", "greenhouse", jobs, now)
    assert con.execute("SELECT count(*) FROM new_postings(1)").fetchone()[0] == 2
    assert con.execute("SELECT count(*) FROM title_match('operational excellence')").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM vw_remote_active").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM text_match('six sigma')").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM taken_down(7)").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM vw_board_health").fetchone()[0] == 0  # no board_runs logged in this test


def test_detail_candidates_since_limits_to_new_postings(tmp_path):
    """The automatic new-posting detail pass only sees postings first seen this run."""
    con = store.connect(str(tmp_path / "t.duckdb"))
    old, run_start = datetime(2026, 9, 14, 8), datetime(2026, 9, 15, 8)
    store.record_board(con, "Acme", "workday", [_job("OLD1"), _job("OLD2")], old)
    store.record_board(con, "Acme", "workday", [_job("OLD1"), _job("OLD2"), _job("NEW1", "Pharmacy Tech")], run_start)
    new_only = store.detail_candidates(con, ["workday"], None, 100, employers=["Acme"], since=run_start)
    assert [r[3] for r in new_only] == ["NEW1"]  # every title, no prefilter
    backlog = store.detail_candidates(con, ["workday"], None, 100, employers=["Acme"])
    assert {r[3] for r in backlog} == {"OLD1", "OLD2", "NEW1"}
    assert store.detail_candidates(con, ["greenhouse"], None, 100, since=run_start) == []


def test_apply_detail_counts_attempts_even_on_an_empty_fetch(tmp_path):
    """A fetch that comes back with no description_text still has to count against the
    budget, or the same dead posting is retried every run forever."""
    con = store.connect(str(tmp_path / "t.duckdb"))
    now = datetime(2026, 9, 14, 8)
    store.record_board(con, "Acme", "workday", [_job("R1")], now)
    pid = con.execute("SELECT posting_id FROM postings").fetchone()[0]
    assert con.execute("SELECT detail_attempts FROM postings WHERE posting_id = ?", [pid]).fetchone()[0] == 0
    store.apply_detail(con, pid, {"description_text": None}, now)
    store.apply_detail(con, pid, {"description_text": None}, now)
    assert con.execute("SELECT detail_attempts FROM postings WHERE posting_id = ?", [pid]).fetchone()[0] == 2


def test_record_detail_error_also_counts_against_the_budget(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    now = datetime(2026, 9, 14, 8)
    store.record_board(con, "Acme", "workday", [_job("R1")], now)
    pid = con.execute("SELECT posting_id FROM postings").fetchone()[0]
    store.record_detail_error(con, pid, now)
    assert con.execute("SELECT detail_attempts FROM postings WHERE posting_id = ?", [pid]).fetchone()[0] == 1


def test_detail_candidates_excludes_exhausted_postings_unless_retrying(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    now = datetime(2026, 9, 14, 8)
    store.record_board(con, "Acme", "workday", [_job("R1"), _job("R2")], now)
    rows = {r[0]: r[1] for r in con.execute("SELECT req_id, posting_id FROM postings").fetchall()}
    for _ in range(store.DETAIL_MAX_ATTEMPTS):
        store.apply_detail(con, rows["R1"], {"description_text": None}, now)

    cands = store.detail_candidates(con, ["workday"])
    assert {c[3] for c in cands} == {"R2"}  # R1 aged out after DETAIL_MAX_ATTEMPTS empty tries

    all_cands = store.detail_candidates(con, ["workday"], retry_exhausted=True)
    assert {c[3] for c in all_cands} == {"R1", "R2"}


def test_detail_candidates_prefers_fewest_attempts_first(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    now = datetime(2026, 9, 14, 8)
    store.record_board(con, "Acme", "workday", [_job("R1"), _job("R2")], now)
    rows = {r[0]: r[1] for r in con.execute("SELECT req_id, posting_id FROM postings").fetchall()}
    store.apply_detail(con, rows["R1"], {"description_text": None}, now)  # R1 tried once, R2 never
    cands = store.detail_candidates(con, ["workday"])
    assert [c[3] for c in cands] == ["R2", "R1"]


# ---------------------------------------------------------------- eightfold
from backend.ats import adapters as A  # noqa: E402

_PCSX_POS = {"id": 1970393556997347, "displayJobId": "200055573", "name": "Cloud & AI Solution Engineer",
             "locations": ["Nigeria, Multiple Locations, Multiple Locations"], "postedTs": 1789480465,
             "department": "Solution Engineering", "workLocationOption": "onsite",
             "atsJobId": "200055573", "positionUrl": "/careers/job/1970393556997347"}
_V2_POS = {"id": 618519792883, "name": "Remote Inside Sales Representative", "location": "Butte, Montana, United States",
           "locations": ["Butte, Montana, United States"], "department": "Sales", "t_update": 1789478940,
           "display_job_id": "2026-261870", "job_description": "Apply Today - a truncated preview",
           "work_location_option": "remote_local", "canonicalPositionUrl": "https://libertymutual.eightfold.ai/careers/job/618519792883"}


def test_eightfold_pcsx_position_maps_thin_list():
    p = A._eightfold_position("https://apply.careers.microsoft.com", _PCSX_POS)
    assert p["req_id"] == "1970393556997347"      # Eightfold id, not the reusable display id
    assert p["title"] == "Cloud & AI Solution Engineer"
    assert p["url"] == "https://apply.careers.microsoft.com/careers/job/1970393556997347"
    assert p["location_primary"] == "Nigeria, Multiple Locations, Multiple Locations"
    assert p["workplace_type"] is None            # Microsoft's list "onsite" is untrusted; the detail decides
    assert p["posted_at"] == date(2026, 9, 15)
    assert p["description_text"] is None          # list never supplies the JD
    assert p["_ef_id"] == 1970393556997347


def test_eightfold_v2_position_keys_on_eightfold_id_and_drops_preview():
    p = A._eightfold_position("https://libertymutual.eightfold.ai", _V2_POS)
    assert p["req_id"] == "618519792883"
    assert p["raw_json"] and "2026-261870" in p["raw_json"]
    assert p["workplace_type"] == "remote"
    assert p["description_text"] is None
    assert "job_description" not in p["raw_json"]


def test_eightfold_detail_fields_fill_description_and_pay():
    d = {"id": 618519792883, "location": "Butte, Montana, United States", "locations": [],
         "work_location_option": None, "t_update": 1789478940,
         "job_description": "<b>Pay</b><br>The range is $85,000 - $105,000 annually."}
    posting = A._eightfold_position("https://libertymutual.eightfold.ai", _V2_POS)
    f = A._eightfold_detail_fields(d, posting)
    assert f["description_text"].startswith("Pay")
    assert (f["pay_min"], f["pay_max"], f["pay_interval"], f["pay_source"]) == (85000, 105000, "year", "text")
    assert f["workplace_type"] == "remote"         # falls back to the list's flag
    assert f["location_primary"] == "Butte, Montana, United States"


def test_eightfold_list_flag_trusted_elsewhere_and_when_microsoft_says_remote():
    assert A._eightfold_position("https://apply.omnicell.com", _PCSX_POS)["workplace_type"] == "onsite"
    remote = dict(_PCSX_POS, workLocationOption="remote")
    assert A._eightfold_position("https://apply.careers.microsoft.com", remote)["workplace_type"] == "remote"


def test_work_site_type_reads_days_in_office():
    assert A._work_site_type("0 days / week in-office \u2013 remote") == "remote"
    assert A._work_site_type("3 days / week in-office") == "hybrid"
    assert A._work_site_type("Fully on-site") == "onsite"
    assert A._work_site_type("On-site") == "onsite"
    assert A._work_site_type(None) is None


def test_eightfold_pcsx_detail_takes_work_site_and_travel():
    d = {"id": 1970393557026746, "location": "United States, Multiple Locations, Multiple Locations",
         "locations": ["United States, Multiple Locations, Multiple Locations"], "workLocationOption": "onsite",
         "department": "Business Program Management", "postedTs": 1791984773,
         "jobDescription": "<p>Lead culture programs.</p>",
         "efcustomTextWorkSite": ["0 days / week in-office \u2013 remote"],
         "efcustomTextRequiredTravel": ["Less than 25%"]}
    posting = {"workplace_type": "onsite", "location_primary": None, "locations": None}  # a row stored before the fix
    f = A._eightfold_pcsx_detail_fields(d, posting)
    assert f["description_text"] == "Lead culture programs."
    assert f["workplace_type"] == "remote"        # the work site overrides the stale stored flag
    assert f["work_site"] == "0 days / week in-office \u2013 remote"
    assert f["travel"] == "Less than 25%"
    hybrid = dict(d, efcustomTextWorkSite=["3 days / week in-office"], efcustomTextRequiredTravel=[])
    f = A._eightfold_pcsx_detail_fields(hybrid, posting)
    assert (f["workplace_type"], f["travel"]) == ("hybrid", None)


def test_eightfold_detail_uses_position_details_for_microsoft(monkeypatch):
    seen = []

    class Resp:
        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    def fake_request(c, method, url, **kw):
        seen.append(url)
        return Resp({"data": {"id": 7, "jobDescription": "JD", "efcustomTextWorkSite": ["Fully on-site"]}})

    monkeypatch.setattr(A, "_request", fake_request)
    row = {"employer": "Microsoft", "identifier_1": "https://apply.careers.microsoft.com", "identifier_2": "microsoft.com"}
    f = A.eightfold_detail(row, {"_ef_id": 7})
    assert "/api/pcsx/position_details?position_id=7&domain=microsoft.com" in seen[0]
    assert (f["workplace_type"], f["work_site"]) == ("onsite", "Fully on-site")


def test_eightfold_registered():
    assert "eightfold" in A.IMPLEMENTED_PLATFORMS and "eightfold" in A.DETAIL_PLATFORMS


def test_eightfold_union_of_two_orders_and_truncation(monkeypatch):
    """Simulates page drift: the timestamp pass misses one posting, the relevance pass
    has it; a board whose union is still short is reported Truncated."""
    calls = []

    def fake_pages(c, url, host, max_pages):
        calls.append(url)
        if "timestamp" in url:
            return [A._eightfold_position(host, {"id": 1, "name": "A"}), A._eightfold_position(host, {"id": 2, "name": "B"})], 3, False
        return [A._eightfold_position(host, {"id": 2, "name": "B"}), A._eightfold_position(host, {"id": 3, "name": "C"})], 3, False

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass

    class FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, url): return FakeResp()

    monkeypatch.setattr(A, "_eightfold_pages", fake_pages)
    monkeypatch.setattr(A, "client", lambda: FakeClient())
    row = {"employer": "X", "identifier_1": "https://x.eightfold.ai", "identifier_2": "x.com"}
    got = A.eightfold_jobs(row)
    assert sorted(p["req_id"] for p in got) == ["1", "2", "3"]
    assert len(calls) == 2 and not getattr(got, "truncated", False)

    # Still short after both passes -> Truncated, so the close pass is skipped.
    monkeypatch.setattr(A, "_eightfold_pages", lambda c, u, h, m: ([A._eightfold_position(h, {"id": 1, "name": "A"})], 5, False))
    got = A.eightfold_jobs(row)
    assert getattr(got, "truncated", False) and len(got) == 1


def test_eightfold_pages_truncated_when_tenant_omits_count(monkeypatch):
    """A tenant that never sends `count` reports total=0 even with a full page of real
    postings; that must never read as 'the whole (empty) board', or the close-pass
    closes every live req on it."""
    monkeypatch.setattr(A, "PAGE_DELAY", 0)
    pages = [
        {"positions": [{"id": i, "name": f"Role {i}"} for i in range(10)]},  # no "count" key
        {"positions": []},
    ]

    class _Client:
        def __init__(self):
            self.calls = 0

        def request(self, method, url, **kw):
            p = pages[min(self.calls, len(pages) - 1)]
            self.calls += 1
            return _Resp(p)

    out, total, truncated = A._eightfold_pages(_Client(), "https://x/api", "https://x", None)
    assert total == 0 and len(out) == 10 and truncated is True


def test_eightfold_pages_empty_board_is_not_truncated(monkeypatch):
    """A genuinely empty board (total=0, no positions at all) is not a clamp failure."""
    monkeypatch.setattr(A, "PAGE_DELAY", 0)

    class _Client:
        def request(self, method, url, **kw):
            return _Resp({"positions": []})

    out, total, truncated = A._eightfold_pages(_Client(), "https://x/api", "https://x", None)
    assert out == [] and total == 0 and truncated is False


# ---------------------------------------------------------------- paylocity
def test_paylocity_positions_map_embedded_pagedata():
    jobs = [{"JobId": 4466374, "JobTitle": "Data Scientist (US)", "LocationName": "LI-Remote",
             "JobLocation": {"Name": "LI-Remote", "Metro": "US, Remote", "Country": "USA"},
             "IsRemote": False, "IsInternal": False, "HiringDepartment": "General & Administrative",
             "PublishedDate": "2026-08-31T14:34:33-05:00",
             "Description": "<p>About Logos</p><p>Salary range $120,000 - $145,000 per year.</p>"},
            {"JobId": 1, "JobTitle": "Internal only", "IsInternal": True, "Description": ""}]
    out = A._paylocity_positions(jobs)
    assert len(out) == 1
    p = out[0]
    assert p["req_id"] == "4466374"
    assert p["url"] == "https://recruiting.paylocity.com/recruiting/jobs/Details/4466374"
    assert p["location_primary"] == "US, Remote" and p["country"] == "USA"
    assert p["workplace_type"] == "remote"          # sniffed from Metro, IsRemote is False
    assert p["posted_at"] == date(2026, 8, 31)
    assert p["description_text"] is None          # embedded Description is a teaser
    assert "paylocity" in A.IMPLEMENTED_PLATFORMS and "paylocity" in A.DETAIL_PLATFORMS


def test_paylocity_detail_strips_header_and_parses_pay():
    html = """<html><body><div class="job-preview-details"><div><a>Apply</a></div>
    <div><span>Job Type</span><span>Full-time</span></div><div>Description</div>
    <div><p>About Logos</p><p>The salary range is $120,000 - $145,000 annually.</p></div></div></body></html>"""
    f = A._paylocity_detail_fields(html, {})
    assert f["description_text"].startswith("About Logos")
    assert f["employment_type"] is not None
    assert (f["pay_min"], f["pay_max"], f["pay_interval"], f["pay_source"]) == (120000, 145000, "year", "text")


def test_paylocity_pagedata_regex_finds_block():
    html = "<script>\nwindow.pageData = {\"Jobs\":[{\"JobId\":5}],\"x\":1};\n</script>"
    m = A._PAYLOCITY_PAGEDATA.search(html)
    assert m and A.json.loads(m.group(1))["Jobs"][0]["JobId"] == 5


def test_lever_text_keeps_the_duties_and_requirements_sections():
    """Lever puts the actual job in `lists`; reading only description + additional captured the wrapper
    (AHEAD's Senior Manager, Enterprise Transformation stored 2,756 chars of blurb and no duties)."""
    from backend.ats.adapters import lever_text
    posting = {
        "descriptionPlain": "AHEAD builds platforms for digital business.",
        "lists": [
            {"text": "Duties/Responsibilities", "content": "<ul><li>Partner with Transformation Leads</li>"
                                                            "<li>Track milestones and dependencies</li></ul>"},
            {"text": "Knowledge, Skills, Abilities", "content": "<ul><li>Strong program management skills</li></ul>"},
            {"text": "", "content": "<ul><li>Unheaded section still counts</li></ul>"},
        ],
        "additionalPlain": "We are an equal opportunity employer.",
    }
    text = lever_text(posting)
    assert "Duties/Responsibilities" in text and "Partner with Transformation Leads" in text
    assert "Track milestones" in text and "Strong program management skills" in text
    assert "Unheaded section still counts" in text
    assert text.startswith("AHEAD builds") and text.rstrip().endswith("equal opportunity employer.")
    assert lever_text({"descriptionPlain": "only a blurb"}) == "only a blurb"
    assert lever_text({}) == ""


def test_oracle_detail_labels_its_sections():
    """Oracle returns description / responsibilities / qualifications as separate unlabelled fields; without
    headings the finder cannot tell the role section from the person section (NFCU's Principal Business Analyst
    stored 5,056 chars that never contained the word 'Responsibilities')."""
    from backend.ats import adapters, normalize as N
    d = {"ExternalDescriptionStr": "<p>About the role.</p>",
         "ExternalResponsibilitiesStr": "<ul><li>Own the intake process</li></ul>",
         "ExternalQualificationsStr": "<ul><li>8 years of experience</li></ul>"}
    parts = [d.get("ExternalDescriptionStr"),
             f"<p>Responsibilities</p>{d['ExternalResponsibilitiesStr']}" if d.get("ExternalResponsibilitiesStr") else None,
             f"<p>Qualifications</p>{d['ExternalQualificationsStr']}" if d.get("ExternalQualificationsStr") else None]
    text = N.html_to_text("\n".join(p for p in parts if p))
    assert "Responsibilities" in text and "Qualifications" in text
    assert "Own the intake process" in text and "8 years of experience" in text
    assert text.index("Responsibilities") < text.index("Qualifications")


# --- Workday CXS `total` clamp (found 2026-09-16) -------------------------------------------------
# Accenture reported total=2000 against facet sums of 44,187 and the close-pass then marked 1,157
# live reqs as taken down. A board we cannot fully enumerate must come back Truncated.

class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _StubClient:
    """Answers the two probe calls _workday_scope makes: unfiltered, then US-filtered."""

    def __init__(self, unfiltered, scoped):
        self._unfiltered, self._scoped = unfiltered, scoped

    def request(self, method, url, **kw):
        applied = (kw.get("json") or {}).get("appliedFacets") or {}
        return self._scoped if applied else self._unfiltered


def _page(total, real_count, multi_valued=None):
    """A CXS page: `real_count` is the single-valued timeType sum, `multi_valued` an inflated location sum."""
    facets = [{"facetParameter": "timeType", "values": [{"count": real_count}]}]
    if multi_valued:
        facets.append({"facetParameter": "locationMainGroup", "values": [{"count": multi_valued}]})
    return _Resp({"total": total, "jobPostings": [], "facets": facets})


def test_workday_clamp_detected_from_a_single_valued_facet():
    from backend.ats import adapters
    clamped = adapters._workday_total_is_clamped
    # Accenture: timeType says 44,216 postings, `total` says 2000.
    assert clamped(_page(2000, 44216).json()) is True
    # Booz Allen's margin is thin but real (2,386 vs 2,000).
    assert clamped(_page(2000, 2386).json()) is True
    assert clamped(_page(254, 254).json()) is False


def test_multi_valued_facets_do_not_fake_a_clamp():
    """A job in three cities counts three times in the location facet. Using the widest facet called
    Autodesk (405 real, 630 in locations) and Guidehouse clamped when both are complete."""
    from backend.ats import adapters
    assert adapters._workday_total_is_clamped(_page(405, 376, multi_valued=630).json()) is False
    assert adapters._workday_total_is_clamped(_page(757, 757, multi_valued=1400).json()) is False


def test_clamp_falls_back_to_the_known_ceiling_without_timetype():
    from backend.ats import adapters
    assert adapters._workday_total_is_clamped({"total": 2000, "facets": []}) is True
    assert adapters._workday_total_is_clamped({"total": 812, "facets": []}) is False


def test_workday_scope_uses_us_filter_when_it_actually_applies():
    from backend.ats import adapters
    c = _StubClient(_page(2000, 44187), _page(710, 710))
    applied, clamped = adapters._workday_scope(c, "u")
    assert applied == {"locationCountry": [adapters.WORKDAY_USA]}
    assert clamped is True, "a clamped board must never be close-passed, even once scoped"


def test_workday_scope_ignores_a_filter_the_tenant_silently_drops():
    """GE Vernova accepts the facet, returns an unchanged total and French locations."""
    from backend.ats import adapters
    c = _StubClient(_page(2000, 44187), _page(2000, 44187))
    applied, clamped = adapters._workday_scope(c, "u")
    assert applied == {} and clamped is True


def test_workday_scope_survives_a_tenant_that_rejects_the_facet():
    """Booz Allen and Sentara answer the US facet with HTTP 400."""
    from backend.ats import adapters
    c = _StubClient(_page(2000, 44187), _Resp({}, status=400))
    applied, clamped = adapters._workday_scope(c, "u")
    assert applied == {} and clamped is True


def test_uncapped_board_is_untouched():
    from backend.ats import adapters
    c = _StubClient(_page(255, 255), _page(90, 90))
    applied, clamped = adapters._workday_scope(c, "u")
    assert applied == {} and clamped is False


# --- The plan executor: every strategy is one execution path with a different plan ------------------

def test_plan_plain_is_one_unfiltered_pull():
    from backend.ats import adapters
    plan, clamped = adapters._workday_plan({"strategy": "plain", "facet_parameter": None, "value_ids": None})
    assert plan == [("all", {})] and clamped is False


def test_plan_country_is_one_filtered_pull():
    from backend.ats import adapters
    plan, clamped = adapters._workday_plan(
        {"strategy": "country", "facet_parameter": "locationCountry", "value_ids": '["US_ID"]'})
    assert plan == [("us", {"locationCountry": ["US_ID"]})] and clamped is False


def test_plan_partition_is_one_pull_per_value():
    """Booz Allen is clamped at 2000 with no country facet, but its job families are 1172/766/445."""
    from backend.ats import adapters
    plan, clamped = adapters._workday_plan(
        {"strategy": "partition", "facet_parameter": "jobFamilyGroup", "value_ids": '["tech","consult","eng"]'})
    assert len(plan) == 3 and clamped is False
    assert plan[0] == ("tech", {"jobFamilyGroup": ["tech"]})


def test_plan_truncate_pulls_what_it_can_and_blocks_the_close_pass():
    from backend.ats import adapters
    plan, clamped = adapters._workday_plan({"strategy": "truncate", "facet_parameter": None, "value_ids": None})
    assert plan == [("all", {})] and clamped is True


def test_no_scope_falls_back_to_a_live_probe():
    from backend.ats import adapters
    assert adapters._workday_plan(None) == (None, False)


# ---- partition plan builder (facets.resolve_partition) ----

def _facet_rows(param, counts, group=""):
    """board_facets-shaped rows: (facet_parameter, group_descriptor, value_id, descriptor, count)."""
    return [(param, group, f"{param}-{i}", f"{param} {i}", c) for i, c in enumerate(counts)]


def test_partition_picks_the_flat_facet_that_covers_every_posting():
    """Booz Allen: 2,386 real postings, job families 1,172 / 766 / 445 + a 3-req remainder."""
    from backend.ats import facets
    rows = _facet_rows("jobFamilyGroup", [1172, 766, 445, 3])
    got = facets.resolve_partition(rows, 2386)
    assert got is not None
    key, ids, note = got
    assert key == "jobFamilyGroup" and len(ids) == 4
    assert "2386" in note


def test_partition_rejects_a_facet_that_leaves_postings_out():
    """The counts must sum to the true total. 1,172 + 766 + 445 = 2,383 against 2,386 means three reqs carry
    no job family, and partitioning on it would close-pass all three as taken down."""
    from backend.ats import facets
    assert facets.resolve_partition(_facet_rows("jobFamilyGroup", [1172, 766, 445]), 2386) is None


def test_partition_rejects_a_multi_valued_facet():
    """A facet summing past the true total counts some postings twice; its values are not a partition."""
    from backend.ats import facets
    assert facets.resolve_partition(_facet_rows("workerSubType", [2000, 1500]), 2386) is None


def test_partition_rejects_a_value_that_is_itself_clamped():
    """A 2,100-req job family would come back clamped at 2,000, so the partition buys nothing."""
    from backend.ats import facets
    assert facets.resolve_partition(_facet_rows("jobFamilyGroup", [2100, 286]), 2386) is None


def test_partition_never_uses_a_location_facet():
    """Locations are multi-valued AND the nested filter key is not the parameter name (the 400 trap)."""
    from backend.ats import facets
    assert facets.resolve_partition(_facet_rows("locationMainGroup", [1200, 1186]), 2386) is None
    assert facets.resolve_partition(_facet_rows("jobFamilyGroup", [1200, 1186], group="City"), 2386) is None


def test_partition_prefers_the_facet_with_the_most_headroom():
    """Two pulls of 1,200 sit closer to the ceiling than four of 600. The extra pulls are cheap; a partition
    value that grows into the clamp costs the whole enumeration."""
    from backend.ats import facets
    rows = _facet_rows("jobFamilyGroup", [1200, 1186]) + _facet_rows("timeType", [600, 600, 600, 586])
    key, ids, _note = facets.resolve_partition(rows, 2386)
    assert key == "timeType" and len(ids) == 4


def test_partition_needs_a_true_total_to_check_against():
    from backend.ats import facets
    assert facets.resolve_partition(_facet_rows("jobFamilyGroup", [1200, 1186]), None) is None


def test_strategy_prefers_country_over_partition():
    """One pull beats many: a partition is only for boards that expose no country facet at all."""
    from backend.ats import facets
    us = ("locationCountry", ["abc"], "United States of America")
    part = ("jobFamilyGroup", ["a", "b"], "note")
    assert facets.strategy_for(True, us, part) == "country"
    assert facets.strategy_for(True, None, part) == "partition"
    assert facets.strategy_for(True, None, None) == "truncate"
    assert facets.strategy_for(False, None, None) == "plain"


def test_workday_true_total_reads_only_timetype():
    from backend.ats import adapters
    assert adapters.workday_true_total(_page(2000, 2386, multi_valued=9999).json()) == 2386
    assert adapters.workday_true_total({"facets": []}) is None


def test_partition_prefers_headroom_over_fewer_pulls():
    """Leidos: Is_Evergreen is 2 pulls with its largest value 137 reqs short of the ceiling; jobFamilyGroup is
    27 pulls with 1,083 of headroom. Staying under the ceiling as the board grows is worth the 25 extra pulls."""
    from backend.ats import facets
    rows = _facet_rows("Is_Evergreen", [1863, 347]) + _facet_rows("jobFamilyGroup", [917] + [1293 // 26] * 25 + [1293 - (1293 // 26) * 25])
    key, ids, _note = facets.resolve_partition(rows, 2210)
    assert key == "jobFamilyGroup"


def test_partition_breaks_a_headroom_tie_on_fewest_pulls():
    from backend.ats import facets
    rows = _facet_rows("jobFamilyGroup", [500, 500, 500, 886]) + _facet_rows("workerSubType", [886, 500, 500, 250, 250])
    key, _ids, _note = facets.resolve_partition(rows, 2386)
    assert key == "jobFamilyGroup"


class _PartitionClient:
    """CXS for a partitioned board: unfiltered probes report `totals` in order; each filtered pull returns
    `per_value` postings for that value."""

    def __init__(self, totals, per_value):
        self._totals, self._per_value, self._probes = list(totals), per_value, 0

    def request(self, method, url, **kw):
        body = kw.get("json") or {}
        applied = body.get("appliedFacets") or {}
        if not applied:
            total = self._totals[min(self._probes, len(self._totals) - 1)]
            self._probes += 1
            return _Resp({"total": 2000, "jobPostings": [],
                          "facets": [{"facetParameter": "timeType", "values": [{"count": total}]}]})
        value = next(iter(applied.values()))[0]
        n = self._per_value[value] if body.get("offset", 0) == 0 else 0
        return _Resp({"total": n, "jobPostings": [
            {"title": f"r{value}-{i}", "bulletFields": [f"{value}-{i}"], "externalPath": f"/{value}/{i}"}
            for i in range(n)]})

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _partition_board(monkeypatch, totals, per_value):
    from backend.ats import adapters
    monkeypatch.setattr(adapters, "client", lambda: _PartitionClient(totals, per_value))
    monkeypatch.setattr(adapters, "PAGE_DELAY", 0)
    row = {"employer": "E", "platform": "workday", "identifier_1": "t", "identifier_2": "wd1",
           "identifier_3": "S"}
    scope = {"strategy": "partition", "facet_parameter": "jobFamilyGroup", "value_ids": '["a","b"]'}
    return adapters.workday_jobs(row, scope=scope)


def test_partitioned_pull_that_covers_the_board_allows_a_close_pass(monkeypatch):
    jobs = _partition_board(monkeypatch, [30, 30], {"a": 18, "b": 12})
    assert len(jobs) == 30 and getattr(jobs, "truncated", False) is False


def test_partitioned_pull_short_of_the_true_total_is_never_close_passed(monkeypatch):
    """A facet leaving 10 reqs uncovered on a still board: the union must be marked truncated, or every one
    of those reqs is closed as taken down."""
    jobs = _partition_board(monkeypatch, [30, 30], {"a": 12, "b": 8})
    assert len(jobs) == 20 and jobs.truncated is True


def test_partition_forgives_a_shortfall_no_larger_than_the_measured_churn(monkeypatch):
    """Booz Allen moved 2,394 -> 2,396 mid-pull and came back two short. Drift, not a coverage gap."""
    jobs = _partition_board(monkeypatch, [2394, 2396], {"a": 1500, "b": 892})
    assert len(jobs) == 2392 and getattr(jobs, "truncated", False) is False


def test_drift_does_not_excuse_a_gap_bigger_than_itself(monkeypatch):
    jobs = _partition_board(monkeypatch, [2394, 2396], {"a": 1500, "b": 850})
    assert len(jobs) == 2350 and jobs.truncated is True


# --- Live clamp check on every pull, independent of a stored board_scope --------------------------

def test_workday_pull_flags_a_live_clamp_even_under_a_plain_scope(monkeypatch):
    """A board discovered under 2,000 (stored scope 'plain'/'country') can grow past the ceiling
    later. `_workday_pull` must catch that on the first page of every pull, not just when the
    board has never been scoped before."""
    from backend.ats import adapters
    monkeypatch.setattr(adapters, "PAGE_DELAY", 0)
    payload = {"total": 2000, "jobPostings": [],
               "facets": [{"facetParameter": "timeType", "values": [{"count": 2600}]}]}

    class _OneShotClient:
        def request(self, method, url, **kw):
            return _Resp(payload)

    out, truncated = adapters._workday_pull(_OneShotClient(), "u", "https://public", {}, None)
    assert out == [] and truncated is True


def test_workday_pull_stops_when_workday_repeats_page_one(monkeypatch):
    """Workday answers an out-of-range offset with page 1 rather than an empty page, so a board
    with no trustworthy `total` (0 or missing) needs its own stop condition."""
    from backend.ats import adapters
    monkeypatch.setattr(adapters, "PAGE_DELAY", 0)
    page = {"total": 0, "jobPostings": [{"bulletFields": [f"R{i}"], "title": f"T{i}", "externalPath": f"/{i}"}
                                        for i in range(20)],
            "facets": []}

    class _RepeatClient:
        def request(self, method, url, **kw):
            return _Resp(page)

    out, truncated = adapters._workday_pull(_RepeatClient(), "u", "https://public", {}, None)
    assert len(out) == 20 and truncated is True  # only page 1's postings kept, loop didn't spin forever


# ---------------------------------------------------------------- USAJobs
_USAJOBS_ITEM = {
    "MatchedObjectId": "812345600",
    "MatchedObjectDescriptor": {
        "PositionID": "AGENCY-26-1234567",
        "PositionTitle": "Program Analyst",
        "PositionURI": "https://www.usajobs.gov/job/812345600",
        "OrganizationName": "Department of Example",
        "PositionLocationDisplay": "Washington DC, District of Columbia",
        "PositionLocation": [{"LocationName": "Washington DC, District of Columbia", "CountryCode": "US"}],
        "JobCategory": [{"Name": "Program Management", "Code": "0340"}],
        "JobGrade": [{"Code": "GS"}],
        "PositionSchedule": [{"Name": "Full-time"}],
        "PositionOfferingType": [{"Name": "Permanent"}],
        "QualificationSummary": "Experience with process improvement and continuous improvement initiatives.",
        "PositionRemuneration": [{"MinimumRange": "89000.00", "MaximumRange": "115000.00",
                                  "RateIntervalCode": "Per Year"}],
        "PublicationStartDate": "2026-09-01",
        "ApplicationCloseDate": "2026-10-01",
        "UserArea": {"Details": {
            "JobSummary": "This position leads process excellence initiatives for the agency.",
            "MajorDuties": ["Leads Lean Six Sigma projects.", "Coordinates capacity planning."],
            "Education": "",
            "Requirements": "Ability to obtain and maintain a Secret clearance.",
            "Evaluations": "",
            "SecurityClearance": "Secret",
            "LowGrade": "9",
            "HighGrade": "11",
            "RemoteIndicator": False,
            "TeleworkEligible": True,
            "OtherInformation": "",
        }},
    },
}


def test_usajobs_position_maps_full_jd_with_no_detail_needed():
    p = A._usajobs_position(_USAJOBS_ITEM["MatchedObjectDescriptor"], _USAJOBS_ITEM["MatchedObjectId"])
    assert p["req_id"] == "812345600"                  # the USAJobs control number (MatchedObjectId)
    assert A._usajobs_position(_USAJOBS_ITEM["MatchedObjectDescriptor"])["req_id"] == "AGENCY-26-1234567"
    assert p["title"] == "Program Analyst"
    assert p["url"] == "https://www.usajobs.gov/job/812345600"
    assert p["location_primary"] == "Washington DC, District of Columbia" and p["country"] == "US"
    assert p["job_family"] == "Department of Example"        # the hiring agency, not job_family in the tier sense
    assert p["job_level"] == "GS-9/11"
    assert (p["pay_min"], p["pay_max"], p["pay_interval"], p["pay_source"]) == (89000, 115000, "year", "ats")
    assert p["employment_type"] == "full_time"
    assert p["posted_at"] == date(2026, 9, 1) and p["posting_end_at"] == date(2026, 10, 1)
    assert "Leads Lean Six Sigma projects." in p["description_text"]
    assert "ability to obtain and maintain a Secret security clearance" in p["description_text"]
    from backend import screen as S
    assert S.clearance_call(p["description_text"])[0] == "sponsored"   # a flag, never a reject
    assert "usajobs" in A.IMPLEMENTED_PLATFORMS and "usajobs" not in A.DETAIL_PLATFORMS


def test_usajobs_clearance_line_skips_no_clearance_values():
    assert A._usajobs_clearance_line("Not Required") is None
    assert A._usajobs_clearance_line("None") is None
    assert A._usajobs_clearance_line("") is None
    assert A._usajobs_clearance_line(None) is None
    assert "obtain and maintain a Top Secret/SCI security clearance" in A._usajobs_clearance_line("Top Secret/SCI")


def test_usajobs_workplace_remote_indicator_wins_over_location_text():
    assert A._usajobs_workplace({"RemoteIndicator": True}, "Washington DC") == "remote"
    assert A._usajobs_workplace({"RemoteIndicator": False}, "Remote - Nationwide") == "remote"
    assert A._usajobs_workplace({}, "Washington DC, District of Columbia") is None


def test_usajobs_jobs_skips_without_credentials_but_never_fails(monkeypatch):
    monkeypatch.delenv("USAJOBS_API_KEY", raising=False)
    monkeypatch.delenv("USAJOBS_EMAIL", raising=False)
    got = A.usajobs_jobs({"employer": "U.S. Government (USAJobs)", "platform": "usajobs"})
    assert got == [] and getattr(got, "truncated", False) is True


def test_usajobs_jobs_paginates_dedupes_and_is_always_truncated(monkeypatch):
    """A keyword result set is never a full board -- even a clean, single-page pull across every
    phrase must come back Truncated, so the close-pass never touches it (see store.py)."""
    monkeypatch.setenv("USAJOBS_API_KEY", "k")
    monkeypatch.setenv("USAJOBS_EMAIL", "e@example.com")
    monkeypatch.setattr(A, "PAGE_DELAY", 0)
    monkeypatch.setattr(A.P, "FUNCTION_PHRASES", ["Process Excellence", "Change Management"])
    monkeypatch.setattr(A.P, "SEPARATE_PASS_PHRASES", [])

    calls = []

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def request(self, method, url, **kw):
            calls.append(kw.get("params", {}).get("Keyword"))
            # Every phrase returns the same one control number -- a real overlap across phrases.
            payload = {"SearchResult": {"SearchResultCountAll": 1,
                                        "SearchResultItems": [{"MatchedObjectDescriptor":
                                                               _USAJOBS_ITEM["MatchedObjectDescriptor"]}]}}
            return _Resp(payload)

    monkeypatch.setattr(A, "client", lambda: _Client())
    got = A.usajobs_jobs({"employer": "U.S. Government (USAJobs)", "platform": "usajobs"})
    assert len(got) == 1                                   # deduped by control number across both phrases
    assert calls == ["Process Excellence", "Change Management"]
    assert getattr(got, "truncated", False) is True


# ---------------------------------------------------------------- USAJobs close-by-date exception
def test_close_expired_postings_leaves_absent_but_unexpired_postings_active(tmp_path):
    """A posting missing from a USAJobs keyword pull is not evidence it's gone -- record_board's
    close-pass must already be a no-op (the adapter always returns Truncated), and
    close_expired_postings must leave anything not yet past its own close date alone."""
    con = store.connect(str(tmp_path / "t.duckdb"))
    t1, t2 = datetime(2026, 9, 14), datetime(2026, 9, 15)
    far_future = N.parse_date("2027-01-01")
    jobs = [N.base(req_id="R1", title="Program Analyst", posting_end_at=far_future)]
    store.record_board(con, "U.S. Government (USAJobs)", "usajobs", jobs, t1, truncated=True)
    # R1 is absent from the next pull, but the pull is truncated so it must not be closed by absence.
    _, _, _, closed = store.record_board(con, "U.S. Government (USAJobs)", "usajobs", [], t2, truncated=True)
    assert closed == 0
    n = store.close_expired_postings(con, store.CLOSE_BY_DATE_PLATFORMS, t2)
    assert n == 0
    assert con.execute("SELECT status FROM postings WHERE req_id = 'R1'").fetchone()[0] == "active"


def test_close_expired_postings_closes_only_past_its_own_close_date(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    t1 = datetime(2026, 9, 14)
    expired = N.parse_date("2026-09-01")     # already past when we check on t1
    still_open = N.parse_date("2026-12-01")
    no_date = None
    jobs = [N.base(req_id="R1", title="Expired", posting_end_at=expired),
            N.base(req_id="R2", title="Still open", posting_end_at=still_open),
            N.base(req_id="R3", title="No date given", posting_end_at=no_date)]
    store.record_board(con, "U.S. Government (USAJobs)", "usajobs", jobs, t1, truncated=True)
    n = store.close_expired_postings(con, store.CLOSE_BY_DATE_PLATFORMS, t1)
    assert n == 1
    rows = {r[0]: r[1] for r in con.execute("SELECT req_id, status FROM postings").fetchall()}
    assert rows == {"R1": "closed", "R2": "active", "R3": "active"}


def test_close_expired_postings_never_touches_other_platforms(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    t1 = datetime(2026, 9, 14)
    expired = N.parse_date("2026-09-01")
    store.record_board(con, "Acme", "workday", [N.base(req_id="R1", title="X", posting_end_at=expired)], t1)
    n = store.close_expired_postings(con, store.CLOSE_BY_DATE_PLATFORMS, t1)
    assert n == 0
    assert con.execute("SELECT status FROM postings WHERE req_id = 'R1'").fetchone()[0] == "active"


def test_connect_caps_duckdb_memory_and_honours_the_env_override(tmp_path, monkeypatch):
    """DuckDB defaults memory_limit to 80% of RAM (6.2 GiB of this 8 GB WSL VM), which leaves too
    little for a stage holding a sentence-transformer encoder in the SAME process -- that combination
    took the VM down on 2026-09-21 during required-embed scoring. connect() caps it, and
    JOBSEARCH_DUCKDB_MEMORY_LIMIT overrides the cap on a bigger machine."""
    monkeypatch.delenv("JOBSEARCH_DUCKDB_MEMORY_LIMIT", raising=False)
    con = store.connect(str(tmp_path / "cap.duckdb"))
    default = con.execute("SELECT current_setting('memory_limit')").fetchone()[0]
    con.close()
    assert default == "3.2 GiB", default          # 3500MB (DuckDB reads MB as 10^6) reported back as GiB

    monkeypatch.setenv("JOBSEARCH_DUCKDB_MEMORY_LIMIT", "1200MB")
    con = store.connect(str(tmp_path / "cap2.duckdb"))
    assert con.execute("SELECT current_setting('memory_limit')").fetchone()[0] == "1.1 GiB"
    con.close()


# ---------------------------------------------------------------- employer location index (Stripe)
class _PageResp:
    def __init__(self, payload=None, text=""):
        self._payload, self.text = payload, text

    def json(self):
        return self._payload


def _stripe_page(listings, table):
    data = {"props": {"pageProps": {"jobIndexData": {"filters": {"locations": table}, "listings": listings}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


def test_indexed_location_reads_remote_and_office_entries():
    from backend.ats import adapters as A
    assert A._indexed_location([{"name": "Remote in United States", "remote": True, "countryCode": "US"}]) == (
        "Remote in United States", ["Remote in United States"], "remote")
    assert A._indexed_location([{"name": "Chicago", "countryCode": "US"}, {"name": "Seattle", "countryCode": "US"}]) == (
        "Chicago", ["Chicago", "Seattle"], "hybrid")
    # remote only outside the US is not a remote offer for a US search -- left to the country rules
    assert A._indexed_location([{"name": "Remote in Canada", "remote": True, "countryCode": "CA"}])[2] is None
    assert A._indexed_location([]) is None


def test_greenhouse_uses_the_employer_index_over_the_thin_feed_location(monkeypatch):
    """2026-09-22: Stripe's feed says "US" for an office-only Chicago role; its careers index says Chicago."""
    from backend.ats import adapters as A
    feed = {"jobs": [{"id": 1, "title": "Ops Lead", "absolute_url": "u1", "location": {"name": "US"}, "content": "x"},
                     {"id": 2, "title": "Ops Lead", "absolute_url": "u2", "location": {"name": "US"}, "content": "x"},
                     {"id": 3, "title": "Ops Lead", "absolute_url": "u3", "location": {"name": "US"}, "content": "x"}]}
    table = [{"name": "Chicago", "countryCode": "US"},
             {"name": "Remote in United States", "remote": True, "countryCode": "US"}]
    page = _stripe_page([{"greenhouseId": 1, "locationIndices": [0]}, {"greenhouseId": 2, "locationIndices": [1, 0]}], table)

    def fake(c, method, url, **kw):
        return _PageResp(text=page) if "stripe.com" in url else _PageResp(payload=feed)
    monkeypatch.setattr(A, "_request", fake)
    jobs = {j["req_id"]: j for j in A.greenhouse_jobs({"identifier_1": "stripe", "employer": "Stripe"})}
    assert (jobs["1"]["location_primary"], jobs["1"]["workplace_type"]) == ("Chicago", "hybrid")
    assert json.loads(jobs["2"]["locations"]) == ["Remote in United States", "Chicago"]
    assert jobs["2"]["workplace_type"] == "remote"
    assert jobs["3"]["location_primary"] == "US"          # not in the index -> the feed's own location


def test_greenhouse_falls_back_to_the_feed_when_the_index_fails(monkeypatch):
    from backend.ats import adapters as A
    feed = {"jobs": [{"id": 1, "title": "Ops Lead", "absolute_url": "u1", "location": {"name": "US"}, "content": "x"}]}

    def fake(c, method, url, **kw):
        if "stripe.com" in url:
            raise RuntimeError("index down")
        return _PageResp(payload=feed)
    monkeypatch.setattr(A, "_request", fake)
    assert A.greenhouse_jobs({"identifier_1": "stripe", "employer": "Stripe"})[0]["location_primary"] == "US"


def test_usajobs_telework_flags_set_the_workplace():
    """2026-09-22: not telework-eligible = full-time in person (onsite); eligible = hybrid; remote wins."""
    from backend.ats import adapters as A
    assert A._usajobs_workplace({"RemoteIndicator": True, "TeleworkEligible": False}) == "remote"
    assert A._usajobs_workplace({"RemoteIndicator": False, "TeleworkEligible": False}) == "onsite"
    assert A._usajobs_workplace({"RemoteIndicator": "false", "TeleworkEligible": "true"}) == "hybrid"


def test_prefilter_covers_every_screen_function_term():
    """A title the screen would pass on its function terms must also be one the detail prefilter
    fetches a JD for; otherwise it can sit in `postings` forever with no JD (2026-09-23: five
    terms were missing). DIRECTIONAL must stay a superset of the prefilter."""
    import re
    from backend import profile as P
    from backend.ats import prefilter
    pre = re.compile(prefilter.DETAIL_TITLE_PATTERN, re.I)
    direc = re.compile(prefilter.DIRECTIONAL_TITLE_PATTERN, re.I)
    missing = [t for t in set(P.TITLE_FUNCTION_TERMS) | set(P.PRECISE_TITLE_TERMS)
               if not pre.search(f"x {t} x")]
    assert missing == []
    for probe in ["Enterprise Operating Model Manager", "Global GTM Business Architect", "Change Manager",
                  "Director of Operational Excellence", "Program Manager"]:
        assert pre.search(probe) and direc.search(probe)
    assert direc.search("Technical Project Manager") and not pre.search("Technical Project Manager")


def test_vw_jd_missing_tags_titles_and_fetchability(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    now = datetime.now()
    jobs = [N.base(req_id="F", title="Enterprise Operating Model Manager"),       # function term
            N.base(req_id="P", title="Senior Manager, Fleet Reporting"),          # prefilter only
            N.base(req_id="D", title="Technical Project Manager"),                # directional only
            N.base(req_id="N", title="Mechanical Thermal Engineer I"),            # none
            N.base(req_id="X", title="Retail Sales Consultant"),                  # directional term, excluded
            N.base(req_id="J", title="Director of Process Excellence",
                   description_text="Lean Six Sigma Black Belt required. " * 10)]  # has a JD: absent
    store.record_board(con, "Acme", "workday", jobs, now)
    store.record_board(con, "Beta", "greenhouse", [N.base(req_id="G", title="Process Owner")], now)
    rows = dict(con.execute("SELECT req_id, title_match FROM vw_jd_missing").fetchall())
    assert rows == {"F": "function", "P": "prefilter", "D": "directional", "N": "none", "X": "none", "G": "function"}
    fetch = dict(con.execute("SELECT req_id, fetchable FROM vw_jd_missing").fetchall())
    assert fetch["F"] is True and fetch["G"] is False  # greenhouse lists carry the JD; no detail adapter
    con.execute("UPDATE postings SET detail_attempts = ? WHERE req_id = 'P'", [store.DETAIL_MAX_ATTEMPTS])
    assert con.execute("SELECT fetchable FROM vw_jd_missing WHERE req_id = 'P'").fetchone()[0] is False


def test_detail_candidates_exclude_pattern(tmp_path):
    """`--detail-pattern directional` = DIRECTIONAL minus DIRECTIONAL_EXCLUDE: a retail sales
    "consultant" never spends backlog budget."""
    from backend.ats import prefilter
    con = store.connect(str(tmp_path / "t.duckdb"))
    store.record_board(con, "Acme", "workday", [_job("A", "Technical Project Manager"),
                                                 _job("B", "Retail Sales Consultant"),
                                                 _job("C", "Mechanical Thermal Engineer I")], datetime(2026, 9, 23))
    got = store.detail_candidates(con, ["workday"], prefilter.DIRECTIONAL_TITLE_PATTERN, 100,
                                  exclude_pattern=prefilter.DIRECTIONAL_EXCLUDE_PATTERN)
    assert [r[3] for r in got] == ["A"]


# ---------------------------------------------------------------- ADP Workforce Now
_ADP_FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "adp")
_ADP_ROW = {"employer": "Agile5 Technologies", "platform": "adp",
            "identifier_1": "18bcd225-9065-4e78-8585-763a250b6ab5", "identifier_2": "19000101_000001"}


def _adp_fixture(name):
    with open(os.path.join(_ADP_FIXTURE_DIR, name), encoding="utf-8") as f:
        return json.load(f)


def test_adp_registered():
    assert "adp" in A.IMPLEMENTED_PLATFORMS and "adp" in A.DETAIL_PLATFORMS


def test_adp_position_maps_external_job_id_title_url_location_workplace_employment_posted():
    item = _adp_fixture("list.json")["jobRequisitions"][2]   # "Part Time Data Scientist" -- ExternalJobID 640823
    p = A._adp_position(_ADP_ROW, item)
    assert p["req_id"] == "640823"
    assert p["title"] == "Part Time Data Scientist (1099 - Contractor)"
    assert p["url"] == ("https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
                         "?cid=18bcd225-9065-4e78-8585-763a250b6ab5&ccId=19000101_000001"
                         "&type=JS&lang=en_US&jobId=640823")
    assert p["location_primary"] == "Fairmont, WV, US"
    assert p["workplace_type"] == "remote"        # one of its three locations is "Remote, US"
    assert p["employment_type"] == "part_time"
    assert p["posted_at"] == date(2026, 9, 4)
    assert p["country"] == "US"
    assert p["description_text"] is None          # list never carries the JD


def test_adp_position_falls_back_to_item_id_without_external_job_id():
    item = {"itemID": "999_1", "requisitionTitle": "No Custom Fields Role",
            "postDate": "2026-09-01T00:00:00.000-04:00", "workLevelCode": {"shortName": "Full Time"},
            "requisitionLocations": [{"nameCode": {"shortName": " Remote, US"}}]}
    p = A._adp_position(_ADP_ROW, item)
    assert p["req_id"] == "999_1"
    assert p["url"].endswith("jobId=999_1")


def test_adp_jobs_maps_every_posting_from_the_fixture(monkeypatch):
    monkeypatch.setattr(A, "PAGE_DELAY", 0)
    fixture = _adp_fixture("list.json")

    class _FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def request(self, method, url, **kw): return _Resp(fixture)

    monkeypatch.setattr(A, "client", lambda: _FakeClient())
    out = A.adp_jobs(_ADP_ROW)
    assert not getattr(out, "truncated", False)
    assert len(out) == 6
    ids = [p["req_id"] for p in out]
    assert "640823" in ids and len(set(ids)) == 6
    remote = next(p for p in out if p["req_id"] == "640823")
    assert remote["workplace_type"] == "remote"


def _adp_one_based_server(items, total=None, calls=None):
    """A fake ADP list endpoint with the REAL paging semantics, verified live 2026-09-28: `$skip`
    is 1-based, so $skip=s&$top=t returns postings s..s+t-1 (1-indexed); $skip=0 behaves like
    $skip=1 but one short ($top-1 postings); a $skip past the end returns a body with no `meta`."""
    import re as _re
    total = len(items) if total is None else total

    class _FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def request(self, method, url, **kw):
            if calls is not None:
                calls.append(url)
            top = int(_re.search(r"\$top=(\d+)", url).group(1))
            skip = int(_re.search(r"\$skip=(\d+)", url).group(1))
            start, stop = (0, top - 1) if skip == 0 else (skip - 1, skip - 1 + top)
            page = items[start:stop]
            if not page:
                return _Resp({"jobRequisitions": []})
            return _Resp({"jobRequisitions": page, "meta": {"totalNumber": total}})
    return _FakeClient


def test_adp_jobs_paginates_one_based_and_stops_on_total_number(monkeypatch):
    """A board bigger than one page (forced by shrinking ADP_PAGE to 3; the fixture board has 6) is
    walked from $skip=1 in steps of $top and stops once the unique count reaches meta.totalNumber."""
    monkeypatch.setattr(A, "PAGE_DELAY", 0)
    monkeypatch.setattr(A, "ADP_PAGE", 3)
    calls = []
    monkeypatch.setattr(A, "client", _adp_one_based_server(_adp_fixture("list.json")["jobRequisitions"], calls=calls))
    out = A.adp_jobs(_ADP_ROW, max_pages=5)
    assert len(calls) == 2
    assert "$top=3" in calls[0] and "$skip=1" in calls[0] and "$skip=4" in calls[1]
    assert len(out) == 6 and len({p["req_id"] for p in out}) == 6
    assert not getattr(out, "truncated", False)


def test_adp_jobs_steps_by_the_page_the_server_sent_when_it_caps_top(monkeypatch):
    """Some tenants cap $top below ADP_PAGE (Lassonde: 20 of 50, 2026-10-24). Stepping $skip by ADP_PAGE
    skipped the rest of every page; stepping by what came back reads the whole board."""
    monkeypatch.setattr(A, "PAGE_DELAY", 0)
    monkeypatch.setattr(A, "ADP_PAGE", 4)
    items = _adp_fixture("list.json")["jobRequisitions"]
    inner = _adp_one_based_server(items)

    class _Capped(inner):
        def request(self, method, url, **kw):
            return inner.request(self, method, url.replace("$top=4", "$top=2"), **kw)

    monkeypatch.setattr(A, "client", _Capped)
    out = A.adp_jobs(_ADP_ROW)
    assert len(out) == 6 and not getattr(out, "truncated", False)


def test_oracle_orc_scopes_parse_identifier_3():
    assert A._oracle_orc_scopes("") == [("all", "")]
    assert A._oracle_orc_scopes("loc:123; kw:remote work") == [
        ("loc:123", "selectedLocationsFacet=123,"), ("kw:remote work", "keyword=remote%20work,")]


def test_workday_filtered_pull_is_not_clamped_by_its_own_facet_counts(monkeypatch):
    """A facet never filters its own counts: a timeType slice of 1,200 still reports the whole board's
    timeType sum (2,461). Judged by that, every slice looked clamped and T-Mobile/Sentara were never
    close-passed. A filtered pull is clamped only at the ceiling."""
    monkeypatch.setattr(A, "PAGE_DELAY", 0)
    page = {"total": 1, "jobPostings": [{"title": "t", "externalPath": "/job/x", "bulletFields": ["R1"]}],
            "facets": [{"facetParameter": "timeType", "values": [{"count": 2000}, {"count": 461}]}]}

    class _C:
        def request(self, method, url, **kw): return _Resp(page)

    _out, clamped = A._workday_pull(_C(), "u", "p", {"timeType": ["a"]}, None)
    assert not clamped
    _out, clamped = A._workday_pull(_C(), "u", "p", {}, None)
    assert clamped


def test_adp_jobs_board_that_is_an_exact_multiple_of_the_page_is_read_in_full(monkeypatch):
    """The regression the 1-based fix exists for: with $skip starting at 0 the first page held
    $top-1 postings, so a board of exactly 2 x $top stopped one short. It must now read all 6."""
    monkeypatch.setattr(A, "PAGE_DELAY", 0)
    monkeypatch.setattr(A, "ADP_PAGE", 3)
    monkeypatch.setattr(A, "client", _adp_one_based_server(_adp_fixture("list.json")["jobRequisitions"]))
    out = A.adp_jobs(_ADP_ROW)
    assert len(out) == 6 and not getattr(out, "truncated", False)


def test_adp_jobs_repeated_items_are_deduplicated_and_a_short_read_is_truncated(monkeypatch):
    """If ADP ever repeats a page (or reports more postings than it serves), the pull keeps one copy
    of each itemID, stops when a page adds nothing new, and comes back Truncated -- never a
    complete-looking list the close-pass would act on."""
    monkeypatch.setattr(A, "PAGE_DELAY", 0)
    items = _adp_fixture("list.json")["jobRequisitions"]
    page = {"jobRequisitions": items[:3], "meta": {"totalNumber": 6}}

    class _FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def request(self, method, url, **kw): return _Resp(page)

    monkeypatch.setattr(A, "client", lambda: _FakeClient())
    out = A.adp_jobs(_ADP_ROW)
    assert len(out) == 3 and len({p["req_id"] for p in out}) == 3
    assert getattr(out, "truncated", False) is True


def test_adp_jobs_truncated_when_max_pages_trips(monkeypatch):
    monkeypatch.setattr(A, "PAGE_DELAY", 0)
    page = {"jobRequisitions": [{"itemID": "1_1", "requisitionTitle": "X", "postDate": None,
                                 "workLevelCode": {}, "requisitionLocations": []}],
            "meta": {"totalNumber": 999}}

    class _FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def request(self, method, url, **kw): return _Resp(page)

    monkeypatch.setattr(A, "client", lambda: _FakeClient())
    out = A.adp_jobs(_ADP_ROW, max_pages=1)
    assert getattr(out, "truncated", False) is True


def test_adp_detail_fills_description_from_requisition_description(monkeypatch):
    detail = _adp_fixture("detail_640823.json")

    class _FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def request(self, method, url, **kw): return _Resp(detail)

    monkeypatch.setattr(A, "client", lambda: _FakeClient())
    posting = A._adp_position(_ADP_ROW, _adp_fixture("list.json")["jobRequisitions"][2])
    fields = A.adp_detail(_ADP_ROW, posting)
    assert fields["description_text"].startswith("About Agile5:")
    assert "Data Scientist" in fields["description_text"]
    assert fields["location_primary"] == "Fairmont, WV, US"
    assert fields["workplace_type"] == "remote"


def test_adp_detail_404_raises_gone(monkeypatch):
    class _FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def request(self, method, url, **kw): return _Resp({}, status=404)

    monkeypatch.setattr(A, "client", lambda: _FakeClient())
    posting = {"req_id": "640823", "location_primary": None, "locations": None,
               "country": None, "workplace_type": None, "employment_type": None, "posted_at": None}
    try:
        A.adp_detail(_ADP_ROW, posting)
        assert False, "expected Gone"
    except A.Gone:
        pass


def test_workplace_type_negation_and_longest_key_first():
    """DEFECT 2026-09-28: 'Office Worker (NOT Remote)' (AT&T, Workday) normalized to `remote` because the
    substring loop hit 'remote' before 'office'. Negation wins, then the longest key; the literal platform
    values seen in the live DB (AT&T, GM, Henry Schein, Solventum) each land where the commute rule needs them."""
    assert N.workplace_type("Office Worker (NOT Remote)") == "onsite"
    assert N.workplace_type("Non-Remote") == "onsite"
    assert N.workplace_type("No remote") == "onsite"
    assert N.workplace_type("Hybrid (not remote)") == "hybrid"
    assert N.workplace_type("Virtual Worker") == "remote"
    assert N.workplace_type("Field") == "onsite"
    assert N.workplace_type("Onsite") == "onsite"
    assert N.workplace_type("On-site") == "onsite"
    assert N.workplace_type("Hybrid") == "hybrid"
    assert N.workplace_type("Remote/Hybrid") == "remote"
    # the location-text fallback honours a negation too, and never invents remote from an office address
    assert N.workplace_type(None, "Dallas, TX (NOT Remote)") == "onsite"
    assert N.workplace_type(None, "USA:TX:Dallas / One AT&T Plaza - Adm:208 S Akard St") is None
    # a stated flag still beats the location sniff
    assert N.workplace_type("Office Worker (NOT Remote)", "Remote - USA") == "onsite"


def _deel_page(obj_json):
    """A Next.js page whose RSC payload is pushed as two JSON-string chunks."""
    payload = 'x:["$","$L19",null,' + obj_json + ']'
    half = len(payload) // 2
    return "".join(f'<script>self.__next_f.push([1,{A.json.dumps(part)}])</script>'
                   for part in (payload[:half], payload[half:]))


def test_deel_tenant_page_maps_job_postings():
    obj = {"careerPageSettings": {"urlSlug": "faithonline"}, "jobPostings": [{
        "id": "f0f5ff7c-536e-4e6f-a0dc-9f6da749c391", "title": "Creator Recruitment & Community",
        "richtextDescription": "$1b", "createdAt": "2026-09-23T16:33:22.802Z",
        "job": {"workArrangementEnum": "REMOTE",
                "jobEmploymentTypes": [{"employmentType": {"name": "Contract"}}],
                "jobLocations": [{"location": {"name": "Remote"}}],
                "jobTeams": [{"team": {"name": "Marketing"}}], "jobDepartments": []},
        "jobPostingPublications": [{"currentState": {"createdAt": "2026-09-24T01:00:00.000Z"}}]}]}
    flight = A._deel_flight(_deel_page(A.json.dumps(obj)))
    out = A._deel_tenant_positions("faithonline", A._deel_array_after(flight, A._DEEL_TENANT_LIST))
    assert len(out) == 1
    p = out[0]
    assert p["url"] == "https://jobs.deel.com/faithonline/job-details/f0f5ff7c-536e-4e6f-a0dc-9f6da749c391/overview"
    assert (p["workplace_type"], p["employment_type"], p["job_family"]) == ("remote", "contract", "Marketing")
    assert p["posted_at"] == date(2026, 9, 24)      # publication date, not draft creation
    assert "richtextDescription" not in A.json.loads(p["raw_json"])
    assert "deel" in A.IMPLEMENTED_PLATFORMS and "deel" in A.DETAIL_PLATFORMS


def test_deel_cms_page_maps_ashby_fed_jobs_and_skips_unlisted():
    obj = {"__component": "product.career-job-listing", "jobs": [
        {"id": 0, "attributes": {"ashby_id": "fc7cec85", "title": "Country Finance Manager",
                                 "location_name": "South Africa", "all_locations": ["South Africa"],
                                 "employment_type": "Full-time", "is_listed": True,
                                 "team_name": "Country Finance Management",
                                 "ashby_published_date": "2026-09-07T08:11:40.247Z"}},
        {"id": 1, "attributes": {"ashby_id": "hidden", "title": "Unlisted", "is_listed": False}}]}
    flight = A._deel_flight(_deel_page(A.json.dumps(obj)))
    jobs = A._deel_array_after(flight, A._DEEL_CMS_LIST)
    out = A._deel_cms_positions("deel", jobs)
    assert [p["req_id"] for p in out] == ["fc7cec85"]
    assert out[0]["employment_type"] == "full_time" and out[0]["posted_at"] == date(2026, 9, 7)


def test_deel_detail_reads_jobposting_jsonld():
    ld = {"@type": "JobPosting", "title": "QA Engineer", "description": "<p>About FaithOnline</p><p>Pay is $40 - $60 per hour.</p>",
          "datePosted": "2026-09-21T10:00:00.000Z", "employmentType": ["CONTRACTOR"],
          "jobLocation": [{"@type": "Place", "address": {"addressLocality": "Remote"}}]}
    html = ('<script type="application/ld+json">{"@type":"BreadcrumbList"}</script>'
            f'<script type="application/ld+json">{A.json.dumps(ld)}</script>')
    f = A._deel_detail_fields(html, {})
    assert f["description_text"].startswith("About FaithOnline")
    assert f["employment_type"] == "contract" and f["posted_at"] == date(2026, 9, 21)
    assert A.json.loads(f["locations"]) == ["Remote"]


def test_deel_detail_without_jsonld_raises_not_gone():
    # A removed posting still returns 200 with no JobPosting block; the board close-pass handles
    # removal, so a missing block must never be read as Gone (a layout change would close them all).
    try:
        A._deel_detail_fields("<html></html>", {})
    except A.Gone:
        raise AssertionError("must not raise Gone")
    except RuntimeError:
        pass


# ---------------------------------------------------------------- 429 backoff (2026-10-22)
class _Resp429:
    def __init__(self, code, headers=None):
        self.status_code, self.headers, self.request = code, headers or {}, None

    def raise_for_status(self):
        pass


class _Client429:
    def __init__(self, codes):
        self.codes, self.calls = list(codes), 0

    def request(self, method, url, **kw):
        self.calls += 1
        return _Resp429(self.codes.pop(0))


def test_request_rides_out_a_429_burst(monkeypatch):
    """Accenture answered ~40% of calls with 429 in bursts of five; three tries lost a page a night."""
    from backend.ats import adapters as A
    sleeps = []
    monkeypatch.setattr(A.time, "sleep", sleeps.append)
    c = _Client429([429] * 5 + [200])
    assert A._request(c, "POST", "u").status_code == 200
    assert c.calls == 6 and sleeps == [2.0, 4.0, 8.0, 16.0, 20.0]


def test_request_gives_up_after_the_429_budget(monkeypatch):
    import httpx
    import pytest
    from backend.ats import adapters as A
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    c = _Client429([429] * A.RETRIES_429)
    with pytest.raises(httpx.HTTPStatusError):
        A._request(c, "GET", "u")
    assert c.calls == A.RETRIES_429


def test_request_5xx_keeps_three_tries(monkeypatch):
    import httpx
    import pytest
    from backend.ats import adapters as A
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    c = _Client429([503] * 10)
    with pytest.raises(httpx.HTTPStatusError):
        A._request(c, "GET", "u")
    assert c.calls == A.RETRIES


# ---------------------------------------------------------------- broken-board list (2026-10-22)
def test_broken_boards_flags_three_bad_runs_in_a_row(tmp_path):
    from datetime import timedelta
    from backend.finder import report
    con = store.connect(str(tmp_path / "t.duckdb"))
    t0 = datetime(2026, 10, 1, 6)
    for day in range(5):
        now = t0 + timedelta(days=day)
        store.log_board(con, "r", "Good", "greenhouse", True, 1.0, now, job_count=10)
        # failing for the last three runs, fine before
        if day < 2:
            store.log_board(con, "r", "Moved", "greenhouse", True, 1.0, now, job_count=5)
        else:
            store.log_board(con, "r", "Moved", "greenhouse", False, 1.0, now, error="404 https://x\nmore")
        store.log_board(con, "r", "Empty", "lever", True, 1.0, now, job_count=0)
        # one bad run is not a streak
        store.log_board(con, "r", "Blip", "workday", day != 4, 1.0, now, job_count=3 if day != 4 else None,
                        error="429" if day == 4 else None)
        store.log_board(con, "r", "Store", "workday", True, 1.0, now, job_count=0, track="bridge")
    # left the registry long ago: never listed
    for day in range(4):
        store.log_board(con, "r", "Gone", "ashby", False, 1.0, t0 - timedelta(days=30 + day), error="404")
    # re-pointed to another platform after its last bad run: drops off at once
    for day in range(3):
        store.log_board(con, "r", "Fixed", "lever", True, 1.0, t0 + timedelta(days=day), job_count=0)
    store.log_board(con, "r", "Fixed", "greenhouse", True, 1.0, t0 + timedelta(days=4), job_count=2)
    boards = store.broken_boards(con)
    assert [(b["employer"], b["kind"], b["streak"]) for b in boards] == [
        ("Moved", "failing", 3), ("Empty", "empty", 5), ("Store", "empty", 5)]
    assert boards[0]["last_good"] == date(2026, 10, 2)
    lines = store.broken_boards_lines(boards)
    assert lines[0] == "Moved (greenhouse): failing 3 runs in a row, last good 10/02 -- 404 https://x"
    assert lines[2].startswith("Store (workday) [bridge]: empty 5 runs in a row, never had postings")
    block = report._broken_boards_block(con)
    assert len(block) == 1 and "Broken boards (3)" in block[0] and "Moved (greenhouse)" in block[0]


def test_broken_boards_empty_when_healthy(tmp_path):
    from backend.finder import report
    con = store.connect(str(tmp_path / "t.duckdb"))
    store.log_board(con, "r", "Good", "greenhouse", True, 1.0, datetime(2026, 10, 1), job_count=1)
    assert store.broken_boards(con) == [] and report._broken_boards_block(con) == []


# ---- US facet resolution (facets.resolve_us) ----

def test_resolve_us_accepts_location_country_group():
    """TD Bank, Gartner and Alteryx nest the country list under a group named "Location Country", not
    "Country"; it must still resolve to the nested locationCountry key (2026-10-22)."""
    from backend.ats import facets
    rows = [("locationMainGroup", "Location Country", "us-id", "United States of America", 900),
            ("locationMainGroup", "Location Country", "ca-id", "Canada", 800)]
    assert facets.resolve_us(rows) == ("locationCountry", ["us-id"], "United States of America")


def test_resolve_us_plain_country_group_unchanged():
    from backend.ats import facets
    rows = [("locationMainGroup", "Country", "us-id", "United States of America", 10)]
    assert facets.resolve_us(rows) == ("locationCountry", ["us-id"], "United States of America")


def test_relocation_place_gets_relocation_flag(monkeypatch):
    """2026-10-23: a posting whose only in-area site is a RELOCATION place is flagged as a move, not a commute."""
    from backend import screen as S
    from backend import profile as P
    monkeypatch.setattr(P, "COMMUTABLE_PLACES", ["nashville, tn"])
    monkeypatch.setattr(P, "RELOCATION_PLACES", ["nashville, tn"])
    monkeypatch.setattr(P, "COMMUTE_FAR_PLACES", [])
    job = S.Listing(source="ats", search_pass="t", title="Senior Data Analyst", company="Acme",
                    location="Nashville, TN", url="", description="Hybrid role in our Nashville office.")
    S.screen(job, skip_tracker=True)
    assert any(f.startswith("local/hybrid via relocation: ") for f in job.flags), job.flags


# ---------------------------------------------------------------- Phenom (2026-10-23)
def _phenom_fake(monkeypatch, total_us=3, page=2, us_name="United States of America", drop=None):
    """A fake /widgets endpoint: `total_us` US jobs, served `page` at a time; `drop` = an index
    the server never returns (to simulate a short enumeration)."""
    bodies = []

    class Resp:
        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    def fake_request(c, method, url, **kw):
        body = kw["json"]
        bodies.append(body)
        assert method == "POST" and url == "https://careers.example.com/widgets"
        agg = [{"field": "country", "value": {us_name: total_us, "India": 5}}]
        if not body["selected_fields"]:
            return Resp({"refineSearch": {"totalHits": total_us + 5, "data": {"jobs": [{}], "aggregations": agg}}})
        jobs = [{"jobId": f"R{i}", "title": f"Business Analyst {i}", "country": us_name,
                 "location": "Reston, Virginia, " + us_name,
                 "multi_location": ["Reston, Virginia, " + us_name], "type": "Full time",
                 "category": "Operations", "postedDate": "2026-10-08T00:00:00.000+0000",
                 "ml_skills": ["x"]} for i in range(total_us) if i != drop]
        frm = body["from"]
        return Resp({"refineSearch": {"totalHits": total_us, "data": {"jobs": jobs[frm:frm + page], "aggregations": agg}}})

    monkeypatch.setattr(A, "_request", fake_request)
    monkeypatch.setattr(A, "PHENOM_PAGE", page)
    monkeypatch.setattr(A, "PAGE_DELAY", 0)
    return bodies


def test_phenom_pages_us_scoped_and_builds_urls(monkeypatch):
    bodies = _phenom_fake(monkeypatch)
    row = {"employer": "Example", "platform": "phenom", "identifier_1": "https://careers.example.com",
           "identifier_2": "global/en", "identifier_3": "EXAGLOBAL"}
    out = A.list_jobs(row)
    assert not getattr(out, "truncated", False)
    assert [p["req_id"] for p in out] == ["R0", "R1", "R2"]
    assert bodies[1]["selected_fields"] == {"country": ["United States of America"]}
    assert (bodies[1]["lang"], bodies[1]["country"]) == ("en_global", "global")
    p = out[0]
    assert p["url"] == "https://careers.example.com/global/en/job/R0/Business-Analyst-0"
    assert (p["country"], p["employment_type"], p["posted_at"]) == ("US", "full_time", date(2026, 10, 8))
    assert "ml_skills" not in A.json.loads(p["raw_json"])
    assert "phenom" in A.IMPLEMENTED_PLATFORMS and "phenom" in A.DETAIL_PLATFORMS


def test_phenom_short_enumeration_is_truncated(monkeypatch):
    _phenom_fake(monkeypatch, total_us=4, page=10, drop=2)
    row = {"employer": "Example", "platform": "phenom", "identifier_1": "careers.example.com",
           "identifier_2": "us/en", "identifier_3": ""}
    assert getattr(A.list_jobs(row), "truncated", False)


def test_phenom_detail_reads_ddo():
    job = {"title": "BA", "description": "<p>Lead process work.</p><p>Pay: $90,000 - $120,000 per year.</p>",
           "qualifications": "<p>Five years.</p>", "multi_location": [{"location": "Remote, United States"}],
           "remote": "Yes", "type": "Full time", "postedDate": "2026-10-01T00:00:00.000+0000"}
    html = f'<script>phApp.ddo = {A.json.dumps({"jobDetail": {"data": {"job": job}}})}; phApp.x = 1;</script>'
    f = A._phenom_detail_fields(html, {})
    assert f["description_text"].startswith("Lead process work.") and "Five years." in f["description_text"]
    assert (f["workplace_type"], f["pay_min"], f["posted_at"]) == ("remote", 90000, date(2026, 10, 1))
    try:
        A._phenom_detail_fields('<script>phApp.ddo = {"jobDetail": {"data": {}}};</script>', {})
    except A.Gone:
        raise AssertionError("an empty record must not read as Gone")
    except RuntimeError:
        pass


# ---------------------------------------------------------------- SuccessFactors RMK (2026-10-23)
_RMK_TABLE = """<span class="paginationLabel">Results <b>1 – 2</b> of <b>3</b></span><table><tbody>
<tr class="data-row"><td class="colTitle"><span class="jobTitle hidden-phone">
<a href="/job/Addison-Business-Analyst-TX/1413524200/" class="jobTitle-link">Business Analyst</a></span></td>
<td class="colLocation"><span class="jobLocation">Addison, TX, US</span></td>
<td class="colDate"><span class="jobDate">Oct 23, 2026</span></td></tr>
<tr class="data-row"><td class="colTitle"><span class="jobTitle hidden-phone">
<a href="/job/Remote-Process-Lead/1413191400/" class="jobTitle-link">Process Lead</a></span></td>
<td class="colLocation"><span class="jobLocation">Remote, VA, US</span></td>
<td class="colDate"><span class="jobDate">Oct 20, 2026</span></td></tr></tbody></table>"""

_RMK_TILES = """<span id="tile-search-results-label">Showing 1 to 1 of 1 Jobs</span><ul>
<li class="job-tile job-id-1413376700" data-url="/job/Rochester-Lead%2C-Systems-Engineering-NY-14606/1413376700/">
<a class="jobTitle-link" href="/job/Rochester-Lead%2C-Systems-Engineering-NY-14606/1413376700/"> Lead, Systems Engineering </a>
<div id="job-1413376700-desktop-section-country-value">US </div>
<div id="job-1413376700-desktop-section-location-value">Rochester, NY, US, 14606 </div></li></ul>"""


def test_rmk_table_and_tile_templates():
    total, out = A._rmk_page(_RMK_TABLE, "https://careers.example.com")
    assert total == 3 and [p["req_id"] for p in out] == ["1413524200", "1413191400"]
    p = out[0]
    assert p["url"] == "https://careers.example.com/job/Addison-Business-Analyst-TX/1413524200/"
    assert (p["location_primary"], p["country"], p["posted_at"]) == ("Addison, TX, US", "US", date(2026, 10, 23))
    assert out[1]["workplace_type"] == "remote"
    total, out = A._rmk_page(_RMK_TILES, "https://jobs.example.com")
    assert total == 1 and out[0]["title"] == "Lead, Systems Engineering"
    assert (out[0]["country"], out[0]["location_primary"]) == ("US", "Rochester, NY, US, 14606")
    assert "successfactors_rmk" in A.IMPLEMENTED_PLATFORMS and "successfactors_rmk" in A.DETAIL_PLATFORMS


def test_rmk_pages_with_country_facet_and_flags_short_pull(monkeypatch):
    urls = []
    second = _RMK_TABLE.replace("1 – 2", "3 – 3").replace("1413524200", "1413000001").split("<tr", 2)
    pages = {0: _RMK_TABLE, 2: second[0] + "<tr" + second[1] + "</tbody></table>"}

    class Resp:
        def __init__(self, text):
            self.text = text

    def fake(c, method, url, **kw):
        urls.append(url)
        start = int(url.rsplit("startrow=", 1)[1])
        return Resp(pages.get(start, "<span>Results <b>0</b> of <b>3</b></span>"))

    monkeypatch.setattr(A, "_request", fake)
    monkeypatch.setattr(A, "PAGE_DELAY", 0)
    row = {"employer": "Example", "platform": "successfactors_rmk", "identifier_1": "careers.example.com",
           "identifier_2": "US", "identifier_3": ""}
    out = A.list_jobs(row)
    assert urls[0] == "https://careers.example.com/search/?q=&optionsFacetsDD_country=US&startrow=0"
    assert len(out) == 3 and not getattr(out, "truncated", False)
    pages[2] = "<span>Results <b>3 – 3</b> of <b>3</b></span>"       # page 2 comes back empty
    assert getattr(A.list_jobs(row), "truncated", False)
    pages[0] = "<html>maintenance</html>"                              # no count at all: raise, never []
    try:
        A.list_jobs(row)
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass


def test_rmk_detail_reads_microdata():
    html = """<span itemprop="jobLocation"><meta itemprop="addressLocality" content="Addison">
    <meta itemprop="addressRegion" content="TX"><meta itemprop="addressCountry" content="US"></span>
    <meta itemprop="datePosted" content="Fri Oct 23 07:00:00 UTC 2026">
    <span itemprop="description" class="jobdescription"><p>Req ID: 1</p><p>Pay $90,000 - $110,000 per year.</p></span>"""
    f = A._rmk_detail_fields(html, {"locations": None})
    assert f["description_text"].startswith("Req ID: 1") and f["pay_min"] == 90000
    assert f["posted_at"] == date(2026, 10, 23)
    assert A.json.loads(f["locations"]) == ["Addison, TX, US"]


# ---------------------------------------------------------------- Avature (2026-10-23)
def _avature_article(job_id, title, loc, posted="02-Feb-2026"):
    return (f'<article class="article article--result"><h3 class="article__header__text__title">'
            f'<a class="link" href="https://careers.example.com/en_US/ex/JobDetail/{title.replace(" ", "-")}/{job_id}">{title}</a></h3>'
            f'<div class="article__header__text__subtitle"><span class="list-item-location">{loc}</span>'
            f'<span class="list-item-ref">Job ID: {job_id}</span><span class="list-item-posted">{posted}</span></div></article>')


def test_avature_pages_by_offset_and_checks_count(monkeypatch):
    jobs = [_avature_article(str(100 + i), f"Analyst {i}", "Cary, North Carolina, United States" if i % 2 else "Tokyo, Japan")
            for i in range(5)]
    urls = []

    class Resp:
        def __init__(self, text):
            self.text = text

    def fake(c, method, url, **kw):
        urls.append(url)
        off = int(url.rsplit("jobOffset=", 1)[1])
        return Resp('<div aria-label="5 results"></div>' + "".join(jobs[off:off + 2]))

    monkeypatch.setattr(A, "_request", fake)
    monkeypatch.setattr(A, "AVATURE_PAGE_DELAY", 0)
    row = {"employer": "Example", "platform": "avature", "identifier_1": "careers.example.com",
           "identifier_2": "en_US/ex", "identifier_3": ""}
    out = A.list_jobs(row)
    assert urls[:2] == ["https://careers.example.com/en_US/ex/SearchJobs/?jobOffset=0",
                        "https://careers.example.com/en_US/ex/SearchJobs/?jobOffset=2"]
    assert [p["req_id"] for p in out] == ["100", "101", "102", "103", "104"] and not getattr(out, "truncated", False)
    assert (out[1]["country"], out[0]["country"], out[0]["posted_at"]) == ("US", None, date(2026, 2, 2))
    assert "avature" in A.IMPLEMENTED_PLATFORMS and "avature" in A.DETAIL_PLATFORMS
    jobs.pop()                                            # the site says 5 but serves 4: never close-pass
    assert getattr(A.list_jobs(row), "truncated", False)
    assert any("jobSort=postedDate" in u for u in urls)  # a short first pass tries the second order


def test_avature_second_order_recovers_a_shifted_posting(monkeypatch):
    """The name-ordered pass misses job 102 (a page shifted mid-pull); the posted-date pass has it."""
    named = [_avature_article(str(i), f"Analyst {i}", "Reston, Virginia, United States") for i in (100, 101, 103)]
    dated = [_avature_article(str(i), f"Analyst {i}", "Reston, Virginia, United States") for i in (103, 102, 101, 100)]

    class Resp:
        def __init__(self, text):
            self.text = text

    def fake(c, method, url, **kw):
        off = int(url.rsplit("jobOffset=", 1)[1])
        jobs = dated if "jobSort=postedDate" in url else named
        return Resp('<div aria-label="4 results"></div>' + "".join(jobs[off:off + 2]))

    monkeypatch.setattr(A, "_request", fake)
    monkeypatch.setattr(A, "AVATURE_PAGE_DELAY", 0)
    row = {"employer": "Example", "platform": "avature", "identifier_1": "https://careers.example.com",
           "identifier_2": "en_US/ex", "identifier_3": ""}
    out = A.list_jobs(row)
    assert sorted(p["req_id"] for p in out) == ["100", "101", "102", "103"]
    assert not getattr(out, "truncated", False)


def test_avature_deloitte_shape_floor_count(monkeypatch):
    art = ('<article class="article--result " data-total="999+"><h3><a class="link" '
           'href="https://apply.example.com/en_US/careers/JobDetail/Senior-Consultant/3728{n}">Senior Consultant</a></h3>'
           '<div class="article__header__text__subtitle"><span> Example US </span> | <span> Consulting </span> | '
           '<span>Multiple Locations</span></div></article>')
    total, floor, out = A._avature_page(art.format(n=51) + art.format(n=52))
    assert (total, floor) == (999, True)
    assert [p["req_id"] for p in out] == ["372851", "372852"] and out[0]["location_primary"] == "Multiple Locations"


def test_avature_detail_jsonld_and_view_fields():
    ld = {"@type": "JobPosting", "description": "<p>Work you'll do</p><p>Pay $100,000 - $140,000 per year.</p>",
          "datePosted": "2026-10-22", "validThrough": "2026-11-30"}
    html = (f'<script type="application/ld+json">{A.json.dumps({"@type": "JobPosting", "title": "x"})}</script>'
            f'<script type="application/ld+json">{A.json.dumps(ld)}</script>'
            '<div class="article__header--locations"><p class="paragraph">Atlanta, Georgia, United States</p></div>')
    f = A._avature_detail_fields(html, {})
    assert f["description_text"].startswith("Work you'll do") and f["pay_min"] == 100000
    assert (f["posted_at"], f["posting_end_at"], f["country"]) == (date(2026, 10, 22), date(2026, 11, 30), "US")
    html = ('<div class="article__content__view__field"><div class="article__content__view__field__label">Work Arrangement</div>'
            '<div class="article__content__view__field__value">Hybrid</div></div>'
            '<div class="article__content__view__field"><div class="article__content__view__field__label">Posted Date</div>'
            '<div class="article__content__view__field__value">30-Sep-2026</div></div>'
            '<div class="article__content__view__field"><div class="article__content__view__field__value">'
            '<p>Job Description</p><p>Lead security programs.</p></div></div>')
    f = A._avature_detail_fields(html, {})
    assert "Lead security programs." in f["description_text"]
    assert (f["workplace_type"], f["posted_at"]) == ("hybrid", date(2026, 9, 30))


# ---------------------------------------------------------------- Jibe (iCIMS career sites)
def _jibe_record(i, **kw):
    d = {"req_id": str(i), "slug": str(i), "language": "en-us", "title": f"Role {i}",
         "full_location": "Tysons Corner, Virginia", "country": "United States", "country_code": "US",
         "tags3": ["Hybrid"], "employment_type": "FULL_TIME", "categories": [{"name": "IT"}],
         "posted_date": "2026-10-20T21:13:00+0000", "salary_min_value": 0, "salary_max_value": 0,
         "description": "<p>Overview</p><ul><li>Map workflows</li></ul>",
         "responsibilities": "<ul><li>Map workflows</li></ul>",
         "qualifications": "<p>The estimated base salary range is $80,000 - $95,000 annually.</p>"}
    d.update(kw)
    return {"data": d}


def _jibe_server(records, page_size, calls=None):
    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def request(self, method, url, params=None, **kw):
            if calls is not None:
                calls.append(params)
            p = params["page"]
            chunk = records[(p - 1) * page_size: p * page_size]
            return _Resp({"jobs": chunk, "totalCount": len(records)})
    return lambda: _C()


def test_jibe_jobs_pages_until_total_and_maps_fields(monkeypatch):
    monkeypatch.setattr(A, "PAGE_DELAY", 0)
    monkeypatch.setattr(A, "JIBE_PAGE", 2)
    recs = [_jibe_record(1), _jibe_record(2, tags3=["Fully Remote"], full_location="Virtual, Minnesota"),
            _jibe_record(3, country="India", country_code="RS", full_location="Gurugram, India")]
    calls = []
    monkeypatch.setattr(A, "client", _jibe_server(recs, 2, calls))
    out = A.jibe_jobs({"employer": "Acme", "identifier_1": "careers.acme.com"})
    assert not getattr(out, "truncated", False)
    assert [p["req_id"] for p in out] == ["1", "2", "3"] and [c["page"] for c in calls] == [1, 2]
    first = out[0]
    assert first["url"] == "https://careers.acme.com/jobs/1?lang=en-us"
    assert first["workplace_type"] == "hybrid" and first["employment_type"] == "full_time"
    assert (first["pay_min"], first["pay_max"], first["pay_source"]) == (80000, 95000, "text")
    assert first["description_text"].count("Map workflows") == 1      # repeated block not appended twice
    assert out[1]["workplace_type"] == "remote"
    assert out[2]["country"] == "IN"                                    # name wins over a bad country_code


def test_jibe_jobs_raises_on_an_unreadable_board(monkeypatch):
    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def request(self, method, url, **kw): return _Resp({"error": "nope"})
    monkeypatch.setattr(A, "client", lambda: _C())
    import pytest
    with pytest.raises(RuntimeError):
        A.jibe_jobs({"employer": "Acme", "identifier_1": "careers.acme.com"})


def test_jibe_jobs_short_read_is_truncated(monkeypatch):
    monkeypatch.setattr(A, "PAGE_DELAY", 0)
    monkeypatch.setattr(A, "JIBE_PAGE", 2)

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def request(self, method, url, params=None, **kw):
            return _Resp({"jobs": [_jibe_record(1)] if params["page"] == 1 else [], "totalCount": 5})
    monkeypatch.setattr(A, "client", lambda: _C())
    out = A.jibe_jobs({"employer": "Acme", "identifier_1": "careers.acme.com"})
    assert getattr(out, "truncated", False)


# ---------------------------------------------------------------- brassring
import html as _html_mod  # noqa: E402


class _BrResp:
    def __init__(self, payload=None, text="", status=200):
        self._p, self.text, self.status_code, self.headers, self.request = payload, text, status, {}, None

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _br_home(cfg):
    return ('<input id="CookieValue" type="hidden" value="ENC" />'
            '<input name="__RequestVerificationToken" type="hidden" value="TOK" />'
            f'<input id="searchResults" type="hidden" value="{_html_mod.escape(json.dumps(cfg))}" />')


_BR_DELTEK_CFG = {"JobFieldsToDisplay": {"JobTitle": "formtext4", "Position3": ["formtext1", "autoreq"]},
                  "LocationCustomSolrFields": "Location,FORMTEXT1"}


def _br_job(n, loc="US-Herndon-VA (Hybrid)", **extra):
    q = {"reqid": str(600000 + n), "autoreq": f"{n}BR", "jobtitle": f"Short {n}", "formtext4": f"Full Title {n}",
         "formtext1": loc, "lastupdated": "04-Nov-2026", **extra}
    return {"Questions": [{"QuestionName": k, "Value": v} for k, v in q.items()],
            "Link": f"https://sjobs.brassring.com/x?partnerid=1&siteid=2&PageType=JobDetails&jobid={600000 + n}"}


def _br_server(cfg, jobs, calls, total=None):
    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def close(self): pass

        def request(self, method, url, params=None, json=None, headers=None, **kw):
            calls.append((url.rsplit("/", 1)[-1], json, headers))
            if method == "GET":
                return _BrResp(text=_br_home(cfg))
            if url.endswith("MatchedJobs"):
                return _BrResp({"JobsCount": len(jobs) if total is None else total,
                                "Jobs": {"Job": jobs[:A.BRASSRING_PAGE]}})
            page = json["pageNumber"]
            return _BrResp({"Jobs": {"Job": jobs[(page - 1) * A.BRASSRING_PAGE: page * A.BRASSRING_PAGE]}})
    return lambda: _C()


_BR_ROW = {"employer": "Deltek", "platform": "brassring", "identifier_1": "1", "identifier_2": "2"}


def test_brassring_pages_by_reqid_and_maps_client_fields(monkeypatch):
    monkeypatch.setattr(A, "PAGE_DELAY", 0)
    monkeypatch.setattr(A, "BRASSRING_PAGE", 2)
    jobs = [_br_job(1), _br_job(2, loc="US (Remote), US-Herndon-VA (Hybrid), US-Tampa-FL"),
            _br_job(3, loc="India-Bangalore (Remote)")]
    calls = []
    monkeypatch.setattr(A, "client", _br_server(_BR_DELTEK_CFG, jobs, calls))
    out = A.brassring_jobs(_BR_ROW)
    assert not getattr(out, "truncated", False)
    assert [p["req_id"] for p in out] == ["1BR", "2BR", "3BR"]
    assert [c[0] for c in calls] == ["Home", "MatchedJobs", "ProcessSortAndShowMoreJobs"]
    assert calls[1][2] == {"RFT": "TOK"} and calls[1][1]["EncryptedSessionValue"] == "ENC"
    assert calls[1][1]["SortType"] == calls[2][1]["SortType"] == "reqid"     # a unique key: pages never overlap
    first = out[0]
    assert first["title"] == "Full Title 1"                                  # JobFieldsToDisplay.JobTitle
    assert first["location_primary"] == "US-Herndon-VA (Hybrid)" and first["workplace_type"] == "hybrid"
    assert first["country"] == "US" and first["posted_at"] == date(2026, 11, 4)
    assert first["url"].endswith("jobid=600001")
    assert json.loads(out[1]["locations"]) == ["US (Remote)", "US-Herndon-VA (Hybrid)", "US-Tampa-FL"]
    assert out[1]["country"] == "US"
    assert out[2]["country"] == "IN" and out[2]["workplace_type"] == "remote"


def test_brassring_prefers_a_formtext_location_over_a_facility_name(monkeypatch):
    cfg = {"JobFieldsToDisplay": {"JobTitle": "jobtitle"}, "LocationCustomSolrFields": "Location,FORMTEXT5"}
    job = _br_job(1, loc="", location="Performance TPC Logistics (0754)", formtext5="Rock Island, Illinois (IL)")
    monkeypatch.setattr(A, "client", _br_server(cfg, [job], []))
    (p,) = A.brassring_jobs(_BR_ROW)
    assert p["location_primary"] == "Rock Island, Illinois (IL)" and p["country"] == "US"
    assert json.loads(p["locations"]) == ["Rock Island, Illinois (IL)"]


def test_brassring_short_read_is_truncated_and_bad_board_raises(monkeypatch):
    monkeypatch.setattr(A, "PAGE_DELAY", 0)
    monkeypatch.setattr(A, "client", _br_server(_BR_DELTEK_CFG, [_br_job(1)], [], total=5))
    assert getattr(A.brassring_jobs(_BR_ROW), "truncated", False)

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def request(self, method, url, **kw):
            return _BrResp(text=_br_home(_BR_DELTEK_CFG)) if method == "GET" else _BrResp({"error": "x"})
    monkeypatch.setattr(A, "client", lambda: _C())
    import pytest
    with pytest.raises(RuntimeError):
        A.brassring_jobs(_BR_ROW)
    with pytest.raises(ValueError):
        A.brassring_jobs({**_BR_ROW, "identifier_2": ""})


def _br_detail_server(responses, calls):
    class _C:
        def close(self): pass

        def request(self, method, url, json=None, **kw):
            calls.append(url.rsplit("/", 1)[-1])
            if method == "GET":
                return _BrResp(text=_br_home(_BR_DELTEK_CFG))
            return responses.pop(0)
    return lambda: _C()


_BR_DETAIL = {"ServiceResponse": {"Jobdetails": {"JobDetailQuestions": [
    {"VerityZone": "lastupdated", "QuestionName": "", "AnswerValue": "25-Sep-2026"},
    {"VerityZone": "formtext3", "QuestionName": "Career Interests", "AnswerValue": "Consulting"},
    {"VerityZone": "formtext31", "QuestionName": "Position Type", "AnswerValue": "FT"},
    {"VerityZone": "formtext5", "QuestionName": "Position Responsibilities",
     "AnswerValue": "<ul><li>Lead BI implementations for clients across the project lifecycle.</li>"
                    "<li>Design dashboards.</li></ul>"},
    {"VerityZone": "formtext6", "QuestionName": "Qualifications",
     "AnswerValue": "<p>5+ years of BI consulting experience with Power BI or similar tools.</p>"
                    "<p>The salary range for this role is $72,000 - $127,000.</p>"},
    {"VerityZone": "formtext15", "QuestionName": "EEO Statement",
     "AnswerValue": "Deltek, Inc. is an Equal Opportunity / Affirmative Action employer and more text here."},
    {"VerityZone": "dateclosed", "QuestionName": "", "AnswerValue": "25-Sep-2027"},
]}}}


def test_brassring_detail_sections_pay_and_gone(monkeypatch):
    monkeypatch.setattr(A, "_br_sessions", {})
    calls = []
    monkeypatch.setattr(A, "client", _br_detail_server(
        [_BrResp(text="err", status=500), _BrResp(text="err", status=500), _BrResp(text="err", status=500),
         _BrResp(_BR_DETAIL), _BrResp({"ServiceResponse": {"Jobdetails": None}})], calls))
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    posting = {"url": "https://sjobs.brassring.com/x?partnerid=1&siteid=2&jobid=623525", "req_id": "11274BR",
               "workplace_type": "remote"}
    d = A.brassring_detail(_BR_ROW, posting)      # three 500s exhaust the first session; a fresh one succeeds
    assert calls.count("Home") == 2
    text = d["description_text"]
    assert text.startswith("Career Interests: Consulting\nPosition Type: FT")
    assert "Position Responsibilities\n" in text and "Qualifications\n" in text
    assert "Equal Opportunity" not in text
    assert (d["pay_min"], d["pay_max"], d["pay_interval"]) == (72000, 127000, "year")
    assert d["employment_type"] == "full_time" and d["workplace_type"] == "remote"
    assert d["posted_at"] == date(2026, 9, 25) and d["posting_end_at"] == date(2027, 9, 25)
    import pytest
    with pytest.raises(A.Gone):
        A.brassring_detail(_BR_ROW, posting)


# ---------------------------------------------------------------- Amazon (keyword search + liveness recheck)
_AMAZON_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "amazon", "search.json")
_AMAZON_ROW = {"employer": "Amazon / AWS", "platform": "amazon", "identifier_1": "www.amazon.jobs",
               "identifier_2": "USA"}


def _amazon_fixture():
    with open(_AMAZON_FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def test_amazon_registered_with_a_liveness_check_and_no_detail():
    assert "amazon" in A.IMPLEMENTED_PLATFORMS and "amazon" in A.RECHECK_PLATFORMS
    assert "amazon" not in A.DETAIL_PLATFORMS      # search.json carries the whole JD


def test_amazon_position_maps_fields_pay_and_workplace_from_the_location_type():
    jobs = _amazon_fixture()["jobs"]
    onsite, virtual, hourly = (A._amazon_position("https://www.amazon.jobs", j) for j in jobs)
    assert onsite["req_id"] == "10577660"
    assert onsite["url"].startswith("https://www.amazon.jobs/en/jobs/10577660/")
    assert onsite["location_primary"] == "Bellevue, Washington, USA" and onsite["country"] == "US"
    assert onsite["workplace_type"] == "onsite"            # type ONSITE, not a text sniff
    assert (onsite["pay_min"], onsite["pay_max"], onsite["pay_interval"]) == (115600, 160000, "year")
    assert onsite["pay_source"] == "text" and onsite["employment_type"] == "full_time"
    assert onsite["posted_at"] is not None
    assert "Basic Qualifications\n" in onsite["description_text"]
    assert "Preferred Qualifications\n" in onsite["description_text"]
    assert virtual["workplace_type"] == "remote"           # type VIRTUAL
    assert (hourly["pay_min"], hourly["pay_max"], hourly["pay_interval"]) == (27, 40, "hour")
    assert hourly["workplace_type"] == "onsite"
    assert "description" not in json.loads(onsite["raw_json"])


def test_amazon_workplace_mixed_or_unknown_location_types_stay_unknown():
    assert A._amazon_workplace([{"type": "ONSITE"}, {"type": "VIRTUAL"}]) is None
    assert A._amazon_workplace([{"type": "SOMETHING_NEW"}]) is None
    assert A._amazon_workplace([]) is None


def test_amazon_pay_takes_the_widest_range_of_the_first_interval():
    text = ("USA, CA, San Diego - 120,900.00 - 163,200.00 USD annually\n"
            "USA, TN, Nashville - 108,800.00 - 146,900.00 USD annually\n"
            "USA, TN, Nashville - 27.21 - 40.91 USD hourly")
    assert A._amazon_pay(text) == (108800, 163200, "year")
    assert A._amazon_pay("no pay here") is None


def test_amazon_jobs_pages_each_phrase_dedupes_and_is_always_truncated(monkeypatch):
    monkeypatch.setattr(A, "AMAZON_PAGE_DELAY", 0)
    monkeypatch.setattr(A, "AMAZON_PAGE", 2)
    monkeypatch.setattr(A.P, "FUNCTION_PHRASES", ["Process Excellence", "Change Management"])
    monkeypatch.setattr(A.P, "SEPARATE_PASS_PHRASES", [])
    jobs = _amazon_fixture()["jobs"]
    calls = []

    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def request(self, method, url, **kw):
            p = kw["params"]
            calls.append((p["base_query"], p["offset"], p["normalized_country_code[]"]))
            page = jobs[p["offset"]:p["offset"] + p["result_limit"]]
            return _Resp({"error": None, "hits": len(jobs), "jobs": page})

    monkeypatch.setattr(A, "client", lambda: _Client())
    got = A.amazon_jobs(_AMAZON_ROW)
    assert getattr(got, "truncated", False) is True       # a keyword result set, never a full board
    assert sorted(p["req_id"] for p in got) == ["10479167", "10529298", "10577660"]
    assert calls == [("Process Excellence", 0, "USA"), ("Process Excellence", 2, "USA"),
                     ("Change Management", 0, "USA"), ("Change Management", 2, "USA")]


def test_amazon_jobs_raises_on_an_api_error(monkeypatch):
    monkeypatch.setattr(A, "AMAZON_PAGE_DELAY", 0)
    monkeypatch.setattr(A.P, "FUNCTION_PHRASES", ["Process Excellence"])
    monkeypatch.setattr(A.P, "SEPARATE_PASS_PHRASES", [])

    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def request(self, method, url, **kw):
            return _Resp({"error": "Cannot return more than 10000 results at once", "hits": 0, "jobs": None})

    monkeypatch.setattr(A, "client", lambda: _Client())
    import pytest
    with pytest.raises(RuntimeError):
        A.amazon_jobs(_AMAZON_ROW)


def test_amazon_alive_raises_gone_on_404(monkeypatch):
    class _Client:
        def __init__(self, code): self.code = code
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def request(self, method, url, **kw):
            assert method == "HEAD"
            return _Resp429(self.code)

    monkeypatch.setattr(A, "client", lambda: _Client(200))
    assert A.check_alive(_AMAZON_ROW, {"req_id": "1", "url": "https://www.amazon.jobs/en/jobs/1"}) is True
    monkeypatch.setattr(A, "client", lambda: _Client(404))
    import pytest
    with pytest.raises(A.Gone):
        A.check_alive(_AMAZON_ROW, {"req_id": "1", "url": "https://www.amazon.jobs/en/jobs/1"})


def test_recheck_unseen_closes_only_on_the_postings_own_gone_signal(tmp_path, monkeypatch):
    """Absent from a keyword search never closes an Amazon posting; its own 404 does, a live page
    refreshes last_seen_at, and an error leaves it alone."""
    from backend.ats import sweep as S
    con = store.connect(str(tmp_path / "t.duckdb"))
    old = datetime(2026, 9, 1)
    jobs = [N.base(req_id=r, title=r, url=f"https://www.amazon.jobs/en/jobs/{r}") for r in ("GONE", "LIVE", "ERR")]
    store.record_board(con, "Amazon / AWS", "amazon", jobs, old, truncated=True)
    _, _, _, closed = store.record_board(con, "Amazon / AWS", "amazon", [], datetime(2026, 9, 2), truncated=True)
    assert closed == 0

    def fake_alive(row, posting):
        if posting["req_id"] == "GONE":
            raise A.Gone("404")
        if posting["req_id"] == "ERR":
            raise RuntimeError("timeout")
        return True

    monkeypatch.setattr(S.adapters, "check_alive", fake_alive)
    monkeypatch.setattr(S.adapters, "PAGE_DELAY", 0)
    n = S.recheck_unseen(con, [_AMAZON_ROW], log=lambda *a: None)
    assert n == 1
    rows = {r[0]: (r[1], r[2]) for r in con.execute("SELECT req_id, status, last_seen_at FROM postings").fetchall()}
    assert rows["GONE"][0] == "closed"
    assert rows["LIVE"][0] == "active" and rows["LIVE"][1] > old
    assert rows["ERR"] == ("active", old)
    # LIVE was just seen, so a second pass the same day leaves it alone (only ERR is asked again).
    asked = []
    monkeypatch.setattr(S.adapters, "check_alive", lambda row, p: asked.append(p["req_id"]) or True)
    S.recheck_unseen(con, [_AMAZON_ROW], log=lambda *a: None)
    assert asked == ["ERR"]


# ---------------------------------------------------------------- Radancy (TalentBrew career sites)
_RADANCY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "radancy")
_RADANCY_ROW = {"employer": "Optum/UnitedHealth Group", "platform": "radancy",
                "identifier_1": "careers.unitedhealthgroup.com", "identifier_2": "6252001",
                "identifier_3": "United States"}


def _radancy_results():
    with open(os.path.join(_RADANCY_DIR, "results.json"), encoding="utf-8") as f:
        return json.load(f)


def test_radancy_registered_as_whole_board_with_detail():
    assert "radancy" in A.IMPLEMENTED_PLATFORMS and "radancy" in A.DETAIL_PLATFORMS
    assert "radancy" not in A.RECHECK_PLATFORMS and "radancy" not in store.CLOSE_BY_DATE_PLATFORMS


def test_radancy_page_maps_req_title_url_location_and_the_remote_label_only():
    out, total, pages = A._radancy_page("https://careers.unitedhealthgroup.com",
                                        _radancy_results()["results"], "US")
    assert (total, pages, len(out)) == (3, 1, 3)
    remote, aide, chaplain = out
    assert remote["req_id"] == "2394632" and remote["title"] == "Data Analyst - Remote"
    assert remote["url"].startswith("https://careers.unitedhealthgroup.com/job/")
    assert remote["location_primary"] == "Eden Prairie, Minnesota" and remote["country"] == "US"
    assert remote["workplace_type"] == "remote"            # the site's own "Remote" label
    assert aide["workplace_type"] is None                  # no label is not evidence of on-site
    assert chaplain["job_family"] == "Amedisys" and chaplain["req_id"] == "2401516"


def test_radancy_page_without_totals_raises():
    import pytest
    with pytest.raises(RuntimeError):
        A._radancy_page("https://x", "<section id='search-results'></section>", "US")


def _radancy_client(pages, calls):
    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def request(self, method, url, **kw):
            p = kw["params"]
            calls.append((p["CurrentPage"], p["FacetFilters[0].ID"], p["FacetFilters[0].Display"]))
            return _Resp({"results": pages[p["CurrentPage"] - 1]})
    return lambda: _Client()


def _radancy_split(total, per_page):
    """The fixture's three <li> served as pages of `per_page`, claiming `total` results."""
    import re as _re
    html = _radancy_results()["results"]
    head, rest = html.split("<ul>", 1)
    items = _re.findall(r"<li>.*?</li>", rest, _re.S)
    npages = -(-len(items) // per_page)
    head = _re.sub(r'data-total-results="\d+"', f'data-total-results="{total}"', head)
    head = _re.sub(r'data-total-pages="\d+"', f'data-total-pages="{npages}"', head)
    return [head + "<ul>" + "".join(items[i:i + per_page]) + "</ul></section></section>"
            for i in range(0, len(items), per_page)]


def test_radancy_jobs_pages_the_country_facet_and_is_a_complete_board(monkeypatch):
    monkeypatch.setattr(A, "RADANCY_PAGE_DELAY", 0)
    calls = []
    monkeypatch.setattr(A, "client", _radancy_client(_radancy_split(3, 2), calls))
    out = A.radancy_jobs(_RADANCY_ROW)
    assert not getattr(out, "truncated", False)            # whole board: the close-pass may run
    assert [p["req_id"] for p in out] == ["2394632", "2401361", "2401516"]
    assert calls == [(1, "6252001", "United States"), (2, "6252001", "United States")]


def test_radancy_jobs_short_of_the_boards_own_count_is_truncated(monkeypatch):
    monkeypatch.setattr(A, "RADANCY_PAGE_DELAY", 0)
    monkeypatch.setattr(A, "client", _radancy_client(_radancy_split(10, 3), []))
    assert getattr(A.radancy_jobs(_RADANCY_ROW), "truncated", False) is True


def test_radancy_jobs_max_pages_is_truncated(monkeypatch):
    monkeypatch.setattr(A, "RADANCY_PAGE_DELAY", 0)
    monkeypatch.setattr(A, "client", _radancy_client(_radancy_split(3, 1), []))
    out = A.radancy_jobs(_RADANCY_ROW, max_pages=1)
    assert getattr(out, "truncated", False) is True and len(out) == 1


def test_radancy_detail_reads_the_json_ld_and_its_remote_location():
    with open(os.path.join(_RADANCY_DIR, "detail.html"), encoding="utf-8") as f:
        html = f.read()
    d = A._radancy_detail_fields(html, {"location_primary": "Eden Prairie, Minnesota"})
    assert d["workplace_type"] == "remote"                 # jobLocation addressCountry "Remote"
    assert d["posted_at"] == date(2026, 10, 5)
    assert (d["pay_min"], d["pay_max"], d["pay_interval"]) == (91700, 163700, "year")
    assert "Primary Responsibilities" in d["description_text"]
    assert json.loads(d["locations"]) == ["Eden Prairie, Minnesota"]


def test_radancy_detail_without_json_ld_is_an_error_not_gone():
    import pytest
    with pytest.raises(RuntimeError):
        A._radancy_detail_fields("<html><body>new markup</body></html>", {})


def test_radancy_jobs_retries_an_empty_middle_page_then_truncates(monkeypatch):
    """2026-11-09: one live page came back empty mid-pull; it must never read as the board's end."""
    monkeypatch.setattr(A, "RADANCY_PAGE_DELAY", 0)
    pages = _radancy_split(3, 1)
    empty = pages[1].split("<ul>")[0] + "<ul></ul></section></section>"
    served = {"n": 0}

    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def request(self, method, url, **kw):
            page = kw["params"]["CurrentPage"]
            if page == 2:
                served["n"] += 1
                return _Resp({"results": pages[1] if served["n"] == 2 else empty})   # empty once, then real
            return _Resp({"results": pages[page - 1]})

    monkeypatch.setattr(A, "client", lambda: _Client())
    out = A.radancy_jobs(_RADANCY_ROW)
    assert not getattr(out, "truncated", False) and len(out) == 3
    monkeypatch.setattr(A, "client", lambda: type("C", (_Client,), {
        "request": lambda self, m, u, **kw: _Resp({"results": empty if kw["params"]["CurrentPage"] == 2
                                                   else pages[kw["params"]["CurrentPage"] - 1]})})())
    out = A.radancy_jobs(_RADANCY_ROW)
    assert getattr(out, "truncated", False) is True and len(out) == 1


# ---------------------------------------------------------------- iCIMS (careers-<tenant>.icims.com)
_ICIMS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "icims")
_ICIMS_ROW = {"employer": "Example Co", "platform": "icims", "identifier_1": "careers-cadmusgroup"}


def _icims_read(name):
    with open(os.path.join(_ICIMS_DIR, name), encoding="utf-8") as f:
        return f.read()


def test_icims_registered_whole_board_with_detail():
    assert "icims" in A.IMPLEMENTED_PLATFORMS and "icims" in A.DETAIL_PLATFORMS


def test_icims_host_accepts_tenant_with_or_without_domain():
    assert A._icims_host({"identifier_1": "careers-peraton"}) == "careers-peraton.icims.com"
    assert A._icims_host({"identifier_1": "https://careers-peraton.icims.com/jobs"}) == "careers-peraton.icims.com"
    import pytest
    with pytest.raises(ValueError):
        A._icims_host({"employer": "x", "identifier_1": ""})


def test_icims_place_parses_country_state_city():
    assert A._icims_place("US-VA-McLean") == ("McLean, VA", "US")
    assert A._icims_place("US-Remote") == ("Remote", "US")
    assert A._icims_place("Somewhere") == ("Somewhere", None)


def test_icims_page_maps_cards_with_no_workplace_guess():
    out, page, pages = A._icims_page("h", _icims_read("search.html"))
    assert (page, pages, len(out)) == (1, 2, 3)
    first, multi, _ = out
    assert first["req_id"] == "3263" and first["title"] == "Senior Data Analyst"
    assert first["url"].startswith("https://careers-cadmusgroup.icims.com/jobs/3263/") and "?" not in first["url"]
    assert first["location_primary"] == "McLean, VA" and first["country"] == "US"
    assert first["posted_at"] == date(2026, 11, 5)
    assert first["description_text"] is None and first["workplace_type"] is None
    assert json.loads(multi["locations"])[:2] == ["Boulder, CO", "Boston, MA"] and len(json.loads(multi["locations"])) == 6


def _icims_client(pages, calls):
    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def request(self, method, url, **kw):
            pr = kw["params"]["pr"]
            calls.append(pr)
            r = _Resp429(200)
            r.text = pages[pr]
            return r
    return lambda: _Client()


def _icims_pages(per_page, total_pages=None):
    """The fixture's three cards re-served `per_page` to a page, header rewritten to 'Page N of M'."""
    import re as _re
    html = _icims_read("search.html")
    head, rest = html.split('<ul class="container-fluid iCIMS_JobsTable">', 1)
    cards = _re.findall(r'<li class="iCIMS_JobCardItem">.*?</li>', rest, _re.S)
    chunks = [cards[i:i + per_page] for i in range(0, len(cards), per_page)]
    m = total_pages or len(chunks)
    return [_re.sub(r"Page 1 of 2", f"Page {n + 1} of {m}", head) + '<ul class="container-fluid iCIMS_JobsTable">'
            + "".join(ch) + "</ul></body></html>" for n, ch in enumerate(chunks)]


def test_icims_jobs_pages_to_the_last_page_and_is_a_complete_board(monkeypatch):
    monkeypatch.setattr(A, "ICIMS_PAGE_DELAY", 0)
    calls = []
    monkeypatch.setattr(A, "client", _icims_client(_icims_pages(2), calls))
    out = A.icims_jobs(_ICIMS_ROW)
    assert not getattr(out, "truncated", False) and len(out) == 3 and calls == [0, 1]


def test_icims_jobs_max_pages_and_duplicate_cards_are_truncated(monkeypatch):
    monkeypatch.setattr(A, "ICIMS_PAGE_DELAY", 0)
    monkeypatch.setattr(A, "client", _icims_client(_icims_pages(1), []))
    assert getattr(A.icims_jobs(_ICIMS_ROW, max_pages=1), "truncated", False) is True
    pages = _icims_pages(2)
    pages[1] = pages[1].replace("</ul>", "") + pages[0].split('<ul class="container-fluid iCIMS_JobsTable">')[1]
    monkeypatch.setattr(A, "client", _icims_client(pages, []))
    assert getattr(A.icims_jobs(_ICIMS_ROW), "truncated", False) is True       # a shifted board


def test_icims_jobs_verification_page_fails_the_board_without_a_bypass(monkeypatch):
    monkeypatch.setattr(A, "ICIMS_PAGE_DELAY", 0)
    monkeypatch.setattr(A, "client", _icims_client(["<html><body>Human Verification required</body></html>"], []))
    import pytest
    with pytest.raises(RuntimeError):
        A.icims_jobs(_ICIMS_ROW)


def test_icims_detail_reads_json_ld_remote_pay_locations_and_ignores_other_employment():
    d = A._icims_detail_fields(_icims_read("job.html"), {})
    assert d["workplace_type"] == "remote"                 # jobLocationType TELECOMMUTE
    assert d["posted_at"] == date(2026, 10, 9)
    assert (d["pay_min"], d["pay_max"], d["pay_interval"]) == (85000, 110000, "year")
    assert d["employment_type"] is None                    # the feed said OTHER
    assert json.loads(d["locations"])[0] == "Arlington, VA" and d["location_primary"] == "Arlington, VA"
    assert d["job_family"] and d["description_text"].startswith("Overview")


def test_icims_detail_without_json_ld_is_an_error_not_gone():
    import pytest
    with pytest.raises(RuntimeError):
        A._icims_detail_fields("<html><body>new markup</body></html>", {})


# ---------------------------------------------------------------- Pinpoint (<slug>.pinpointhq.com)
_PINPOINT_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "pinpoint", "postings.json")
_PINPOINT_ROW = {"employer": "Example Co", "platform": "pinpoint", "identifier_1": "example"}


def test_pinpoint_registered_whole_board_without_detail():
    assert "pinpoint" in A.IMPLEMENTED_PLATFORMS and "pinpoint" not in A.DETAIL_PLATFORMS


def test_pinpoint_jobs_map_text_pay_and_the_explicit_workplace_field(monkeypatch):
    with open(_PINPOINT_FIXTURE, encoding="utf-8") as f:
        payload = json.load(f)

    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def request(self, method, url, **kw):
            assert url == "https://example.pinpointhq.com/postings.json"
            return _Resp(payload)

    monkeypatch.setattr(A, "client", lambda: _Client())
    out = A.pinpoint_jobs(_PINPOINT_ROW)
    assert not getattr(out, "truncated", False) and len(out) == 2
    remote, hybrid = out
    assert remote["workplace_type"] == "remote" and hybrid["workplace_type"] == "hybrid"
    assert remote["req_id"] == "590483" and remote["url"].startswith("https://example.pinpointhq.com/")
    assert (remote["pay_min"], remote["pay_max"], remote["pay_interval"], remote["pay_source"]) == (140000, 168000, "year", "ats")
    assert hybrid["pay_min"] is None and hybrid["pay_source"] is None        # compensation_visible is false
    assert remote["employment_type"] == "full_time" and remote["posted_at"] is None
    assert remote["job_family"] == "Engineering / Product / Design"
    assert "Primary Responsibilities" in remote["description_text"] and "Qualifications" in remote["description_text"]
    assert "benefits" not in json.loads(remote["raw_json"])


def test_pinpoint_unknown_workplace_value_is_left_unknown():
    j = {"id": 1, "title": "t", "workplace_type": "something_new", "location": {}}
    assert A._pinpoint_position(j)["workplace_type"] is None


def test_pinpoint_jobs_without_a_data_list_raises(monkeypatch):
    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def request(self, method, url, **kw): return _Resp({"error": "nope"})

    monkeypatch.setattr(A, "client", lambda: _Client())
    import pytest
    with pytest.raises(RuntimeError):
        A.pinpoint_jobs(_PINPOINT_ROW)


# ---------------------------------------------------------------- UKG Pro Recruiting (UltiPro job boards)
_UKG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "ukg")
_UKG_ROW = {"employer": "Example Co", "platform": "ukg", "identifier_1": "ABC1000XYZ",
            "identifier_2": "00000000-1111-2222-3333-444444444444", "identifier_3": "recruiting2.ultipro.com"}


def _ukg_read(name):
    with open(os.path.join(_UKG_DIR, name), encoding="utf-8") as f:
        return f.read()


def test_ukg_registered_whole_board_with_detail():
    assert "ukg" in A.IMPLEMENTED_PLATFORMS and "ukg" in A.DETAIL_PLATFORMS


def test_ukg_board_host_defaults_and_requires_a_guid():
    assert A._ukg_board({"identifier_1": "T", "identifier_2": "G"})[0] == "recruiting.ultipro.com"
    assert A._ukg_board({"identifier_1": "T", "identifier_2": "G", "identifier_3": "recruiting2"})[0] == "recruiting2.ultipro.com"
    import pytest
    with pytest.raises(ValueError):
        A._ukg_board({"employer": "x", "identifier_1": "T", "identifier_2": ""})


def _ukg_client(payloads, calls):
    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def request(self, method, url, **kw):
            assert method == "POST" and url.startswith("https://recruiting2.ultipro.com/ABC1000XYZ/JobBoard/")
            skip = kw["json"]["opportunitySearch"]["Skip"]
            calls.append(skip)
            return _Resp(payloads[len(calls) - 1])
    return lambda: _Client()


def test_ukg_jobs_map_fields_and_only_type_two_is_remote(monkeypatch):
    monkeypatch.setattr(A, "UKG_PAGE_DELAY", 0)
    calls = []
    monkeypatch.setattr(A, "client", _ukg_client([json.loads(_ukg_read("search.json"))], calls))
    out = A.ukg_jobs(_UKG_ROW)
    assert not getattr(out, "truncated", False) and len(out) == 3 and calls == [0]
    first = out[0]
    assert first["workplace_type"] == "remote"                       # JobLocationType 2
    assert all(p["workplace_type"] is None for p in out[1:])         # None and 0/1 stay unknown
    assert first["url"].endswith(f"OpportunityDetail?opportunityId={first['req_id']}")
    assert first["country"] == "US" and first["posted_at"] is not None and first["description_text"] is None
    assert "," in first["location_primary"]


def test_ukg_jobs_pages_by_skip_and_a_short_pull_is_truncated(monkeypatch):
    monkeypatch.setattr(A, "UKG_PAGE_DELAY", 0)
    base = json.loads(_ukg_read("search.json"))
    a = dict(base, opportunities=base["opportunities"][:2], totalCount=3)
    b = dict(base, opportunities=base["opportunities"][2:], totalCount=3)
    calls = []
    monkeypatch.setattr(A, "client", _ukg_client([a, b], calls))
    out = A.ukg_jobs(_UKG_ROW)
    assert calls == [0, 2] and len(out) == 3 and not getattr(out, "truncated", False)
    short = dict(base, opportunities=base["opportunities"][:2], totalCount=5)
    monkeypatch.setattr(A, "client", _ukg_client([short, dict(short, opportunities=[])], []))
    assert getattr(A.ukg_jobs(_UKG_ROW), "truncated", False) is True


def test_ukg_jobs_without_an_opportunities_list_raises(monkeypatch):
    import pytest
    monkeypatch.setattr(A, "client", _ukg_client([{"error": "x"}], []))
    with pytest.raises(RuntimeError):
        A.ukg_jobs(_UKG_ROW)


def test_ukg_detail_reads_the_embedded_record_and_pay():
    d = A._ukg_detail_fields(_ukg_read("detail.html"), {})
    assert d["description_text"] and (d["pay_min"], d["pay_max"], d["pay_interval"], d["pay_source"]) == (95000, 120000, "year", "ats")
    assert "workplace_type" not in d                                  # that record's JobLocationType is None


def test_ukg_detail_shell_page_is_an_error_never_gone():
    import pytest
    with pytest.raises(RuntimeError):
        A._ukg_detail_fields("<html><title data-bind='text: title()'></title></html>", {})


def test_ukg_places_fall_back_to_the_localized_name_and_keep_the_country():
    names, country = A._ukg_places({"Locations": [{"LocalizedName": "Remote", "LocalizedDescription": None,
                                                    "Address": {"City": None, "State": None,
                                                                "Country": {"Code": "USA"}}}]})
    assert names == ["Remote"] and country == "US"
    assert A._ukg_places({"Locations": []}) == ([], None)


# ---------------------------------------------------------------- stale SuccessFactors listings (2026-11-11, #570)
def test_rmk_listing_age_flags_old_ids_only(tmp_path):
    from backend.ats import listing_age as LA
    anchor = 1_449_695_000
    assert LA.rmk_age_days(1_355_475_200, anchor) == (anchor - 1_355_475_200) // LA.RMK_IDS_PER_DAY  # ~314 days
    assert LA.rmk_age_days("not-a-number", anchor) is None and LA.rmk_age_days(1_000, None) is None
    assert LA.rmk_age_days(anchor + 5, anchor) == 0
    assert LA.verify_note(LA.RMK_STALE_DAYS - 1) is None
    assert "~10 mo" in LA.verify_note(314)

    con = store.connect(str(tmp_path / "t.duckdb"))
    t = datetime(2026, 11, 11)
    store.record_board(con, "L3Harris", "successfactors_rmk", [_job("1355475200"), _job(str(anchor))], t)
    store.record_board(con, "Acme", "workday", [_job("1")], t)  # a non-RMK id is never dated
    ids = {r[1]: r[0] for r in con.execute("SELECT posting_id, req_id FROM postings").fetchall()}
    flagged = LA.stale_listings(con, list(ids.values()))
    assert set(flagged) == {ids["1355475200"]}
    assert LA.stale_listings(con, []) == {}
