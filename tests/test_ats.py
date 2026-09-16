"""Unit tests for the ATS ingestion layer — no network, temp DuckDB file."""
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
    assert p["workplace_type"] == "onsite"
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
