"""Bridge-role track (sprint plan §31) -- location-check table tests (§31.3) and the close-pass
data-loss guard (§31.4), written FIRST per the branch's process instructions. No network."""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.ats import bridge, store  # noqa: E402
from backend.ats.normalize import base as job  # noqa: E402


# ---------------------------------------------------------------- location_matches (§31.3)
LOCATION_CASES = [
    # (location_text, place_city, place_state, expected)
    ("Springfield, XX", "Springfield", "XX", True),
    ("springfield, xx", "Springfield", "XX", True),          # case-insensitive
    ("Prince Springfield, XX", "Springfield", "XX", False),  # whole-token city, not a tail match
    ("Springfield, YY", "Springfield", "XX", False),         # same city, wrong state
    ("New Springfield, XX", "Springfield", "XX", False),     # a different, longer city name
    ("(USA) XX Springfield 01234 Store #4", "Springfield", "XX", True),
    ("XX - Central - Springfield", "Springfield", "XX", True),
    ("239 - Springfield, XX", "Springfield", "XX", True),
    ("239 - Prince Springfield, XX", "Springfield", "XX", False),
    ("Rivertown, YY", "Springfield", "XX", False),
    ("", "Springfield", "XX", False),
    (None, "Springfield", "XX", False),
    ("Springfield, XX; Rivertown, YY", "Rivertown", "YY", True),   # multi-location string
    ("Fully Remote", "Remote", "remote", True),
    ("Remote - USA", "Remote", "remote", True),
    ("Springfield, XX", "Remote", "remote", False),
]


def test_location_matches_table():
    for text, city, state, expected in LOCATION_CASES:
        assert bridge.location_matches(text, city, state) is expected, (text, city, state)


def test_parse_place():
    assert bridge.parse_place("Springfield, XX") == ("Springfield", "XX")
    assert bridge.parse_place("Remote") == ("Remote", "")


def test_voice_flag():
    assert bridge.voice_flag("Cashier") == "high"
    assert bridge.voice_flag("Overnight Stocker") == "low"
    assert bridge.voice_flag("Assistant Store Manager") == ""
    assert bridge.voice_flag("Call Centre Representative") == "high"
    assert bridge.voice_flag("Warehouse Associate") == "low"
    assert bridge.voice_flag("Teller") == "high"
    assert bridge.voice_flag("Payroll Administrator") == "low"
    assert bridge.voice_flag("IT Consultant") == ""            # bare "consultant" is not a signal
    assert bridge.voice_flag("Sales Consultant") == "high"      # ...but "sales" + "consultant" is
    assert bridge.voice_flag("Auto Detailer") == "low"
    # a title matching both tables is HIGH
    assert bridge.voice_flag("Overnight Stock Sales Associate") == "high"


def test_voice_sort_order_low_first_high_last():
    order = bridge.VOICE_SORT_ORDER
    assert order["low"] < order[""] < order["high"]


# ---------------------------------------------------------------- close-pass guard (§31.4)
def _bjob(req, title="Cashier", places=None):
    j = job(req_id=req, title=title, url=f"https://x/{req}", location_primary="Springfield, XX")
    j["_bridge_places"] = places or ["Springfield, XX"]
    return j


def test_bridge_pull_never_closes_a_place_it_did_not_look_at(tmp_path):
    """A ring-2 place not attempted this run must never be closed just because it's absent from
    a ring-1-only pull's results."""
    con = store.connect(str(tmp_path / "t1.duckdb"))
    t1 = datetime(2026, 9, 20, 10)
    t2 = datetime(2026, 9, 21, 10)

    # Run 1: two places attempted, both succeed. R1 matches Springfield (ring1), R2 matches
    # Rivertown (ring2).
    j1 = _bjob("R1", places=["Springfield, XX"])
    j2 = _bjob("R2", title="Stocker", places=["Rivertown, YY"])
    store.record_bridge_board(con, "Big Box", "workday", [j1, j2], t1,
                              attempted_places={"Springfield, XX", "Rivertown, YY"},
                              place_status={"Springfield, XX": True, "Rivertown, YY": True})

    # Run 2 (ring-1 only): only Springfield attempted, R1 still there, R2 (Rivertown, ring2) is
    # absent from the pull entirely because ring2 was never queried this run.
    store.record_bridge_board(con, "Big Box", "workday", [j1], t2,
                              attempted_places={"Springfield, XX"},
                              place_status={"Springfield, XX": True})

    rows = {r[0]: r[1] for r in con.execute(
        "SELECT req_id, status FROM postings WHERE employer = 'Big Box'").fetchall()}
    assert rows["R1"] == "active"
    assert rows["R2"] == "active", "ring-2 posting must not be closed by a ring-1-only run"


def test_bridge_pull_never_closes_a_place_that_returned_zero_rows(tmp_path):
    """Orchestrator audit 2026-09-21, letter D: a place with zero matched rows this run -- whether
    the query genuinely emptied or the response was merely transient -- must close NOTHING for
    that place. A clean-but-empty pull is not evidence, so this now stays active; staleness
    (letter E) is what eventually retires a posting nothing keeps re-confirming."""
    con = store.connect(str(tmp_path / "t2.duckdb"))
    t1 = datetime(2026, 9, 20, 10)
    t2 = datetime(2026, 9, 21, 10)
    j1 = _bjob("R1")
    store.record_bridge_board(con, "Big Box", "workday", [j1], t1,
                              attempted_places={"Springfield, XX"}, place_status={"Springfield, XX": True})
    # Run 2: same place attempted and succeeded (no error, no page-cap), but R1 no longer comes
    # back -- ZERO rows for this place this run.
    logged = []
    store.record_bridge_board(con, "Big Box", "workday", [], t2,
                              attempted_places={"Springfield, XX"}, place_status={"Springfield, XX": True},
                              log=logged.append)
    status = con.execute("SELECT status FROM postings WHERE req_id = 'R1'").fetchone()[0]
    assert status == "active", "a zero-row place pull must never close anything by itself"
    assert any("0 rows for" in line and "Springfield, XX" in line for line in logged)


def test_bridge_pull_closes_a_posting_when_other_matches_confirm_the_place_is_alive(tmp_path):
    """Contrast case for letter D: the place pull is NOT empty (it matched a DIFFERENT posting),
    so R1's genuine absence from an otherwise-live place still closes it."""
    con = store.connect(str(tmp_path / "t2b.duckdb"))
    t1 = datetime(2026, 9, 20, 10)
    t2 = datetime(2026, 9, 21, 10)
    j1 = _bjob("R1")
    store.record_bridge_board(con, "Big Box", "workday", [j1], t1,
                              attempted_places={"Springfield, XX"}, place_status={"Springfield, XX": True})
    j2 = _bjob("R2", title="Greeter")
    store.record_bridge_board(con, "Big Box", "workday", [j2], t2,
                              attempted_places={"Springfield, XX"}, place_status={"Springfield, XX": True})
    status = con.execute("SELECT status FROM postings WHERE req_id = 'R1'").fetchone()[0]
    assert status == "closed"


def test_bridge_pull_does_not_close_when_the_place_failed_or_truncated(tmp_path):
    con = store.connect(str(tmp_path / "t3.duckdb"))
    t1 = datetime(2026, 9, 20, 10)
    t2 = datetime(2026, 9, 21, 10)
    j1 = _bjob("R1")
    store.record_bridge_board(con, "Big Box", "workday", [j1], t1,
                              attempted_places={"Springfield, XX"}, place_status={"Springfield, XX": True})
    # Run 2: place attempted but the pull FAILED / hit the page cap -- place_status False.
    store.record_bridge_board(con, "Big Box", "workday", [], t2,
                              attempted_places={"Springfield, XX"}, place_status={"Springfield, XX": False})
    status = con.execute("SELECT status FROM postings WHERE req_id = 'R1'").fetchone()[0]
    assert status == "active", "a failed/truncated place must never close anything it covers"


def test_bridge_posting_matched_by_two_places_stays_open_while_either_returns_it(tmp_path):
    con = store.connect(str(tmp_path / "t4.duckdb"))
    t1 = datetime(2026, 9, 20, 10)
    t2 = datetime(2026, 9, 21, 10)
    j1 = _bjob("R1", places=["Springfield, XX", "Rivertown, YY"])
    store.record_bridge_board(con, "Big Box", "workday", [j1], t1,
                              attempted_places={"Springfield, XX", "Rivertown, YY"},
                              place_status={"Springfield, XX": True, "Rivertown, YY": True})
    # Run 2: only matched under Rivertown now (Springfield query no longer returns it), both
    # places attempted and succeeded.
    j1b = _bjob("R1", places=["Rivertown, YY"])
    store.record_bridge_board(con, "Big Box", "workday", [j1b], t2,
                              attempted_places={"Springfield, XX", "Rivertown, YY"},
                              place_status={"Springfield, XX": True, "Rivertown, YY": True})
    status = con.execute("SELECT status, bridge_place FROM postings WHERE req_id = 'R1'").fetchone()
    assert status[0] == "active"
    assert status[1] == "Rivertown, YY"


def test_fit_track_whole_board_sweep_does_not_close_bridge_rows_and_vice_versa(tmp_path):
    con = store.connect(str(tmp_path / "t5.duckdb"))
    t1 = datetime(2026, 9, 20, 10)
    t2 = datetime(2026, 9, 21, 10)
    # Same employer/platform, two rows: a bridge (place-scoped) row and a fit (whole-board) row.
    bj = _bjob("R1")
    store.record_bridge_board(con, "Big Box", "workday", [bj], t1,
                              attempted_places={"Springfield, XX"}, place_status={"Springfield, XX": True})
    fj = job(req_id="R1", title="Corporate Analyst", url="https://x/fit/R1")
    store.record_board(con, "Big Box", "workday", [fj], t1, track="fit")

    # A fit whole-board sweep that no longer sees R1 (fit track) must close only the FIT row.
    store.record_board(con, "Big Box", "workday", [], t2, track="fit")
    # A bridge run against Springfield again -- R1 no longer comes back, but (letter D) the place
    # itself is confirmed alive by a DIFFERENT match, so R1's genuine absence still closes it.
    decoy = _bjob("R2", title="Greeter")
    store.record_bridge_board(con, "Big Box", "workday", [decoy], t2,
                              attempted_places={"Springfield, XX"}, place_status={"Springfield, XX": True})

    rows = con.execute("SELECT track, status FROM postings WHERE employer = 'Big Box' AND req_id = 'R1' "
                       "ORDER BY track").fetchall()
    assert dict(rows) == {"bridge": "closed", "fit": "closed"}


def test_bridge_and_fit_posting_ids_never_collide_for_the_same_req():
    """A bridge row and a fit row for the SAME employer/platform/req_id must be different
    postings.posting_id values -- otherwise one track's upsert would silently overwrite the
    other's row (sprint plan §31.2's 'two rows, one employer' case)."""
    fit_id = store.posting_id("Big Box", "workday", "R1", track="fit")
    bridge_id = store.posting_id("Big Box", "workday", "R1", track="bridge")
    assert fit_id != bridge_id
    # The default (no track argument) is byte-for-byte the old formula -- every existing
    # posting_id in the live DB must keep hashing the same way.
    assert store.posting_id("Big Box", "workday", "R1") == fit_id


# ---------------------------------------------------------------- separation from the fit pipeline (§31.5)
_LONG_DESC = "Process excellence and operational leadership. " * 30  # >= 800 chars for the label-set views


def _seed_fit_and_bridge(con, now):
    """One fit posting and one bridge posting, same employer, distinct req_ids."""
    fit = job(req_id="F1", title="Process Excellence Manager", url="https://x/fit/F1",
              description_text=_LONG_DESC)
    store.record_board(con, "Acme Corp", "workday", [fit], now, track="fit")
    br = _bjob("B1", title="Cashier")
    br["description_text"] = _LONG_DESC
    store.record_bridge_board(con, "Acme Corp", "workday", [br], now,
                              attempted_places={"Springfield, XX"}, place_status={"Springfield, XX": True})
    fit_pid = store.posting_id("Acme Corp", "workday", "F1", track="fit")
    bridge_pid = store.posting_id("Acme Corp", "workday", "B1", track="bridge")
    return fit_pid, bridge_pid


def test_bridge_excluded_from_screening(tmp_path):
    from backend.finder import pipeline
    con = store.connect(str(tmp_path / "t6.duckdb"))
    now = datetime(2026, 9, 21, 10)
    fit_pid, bridge_pid = _seed_fit_and_bridge(con, now)
    pipeline.screen(con, full=True, log=lambda *a, **k: None)
    screened = {r[0] for r in con.execute("SELECT posting_id FROM screens").fetchall()}
    assert fit_pid in screened
    assert bridge_pid not in screened


def test_bridge_excluded_from_detail_candidates(tmp_path):
    con = store.connect(str(tmp_path / "t7.duckdb"))
    now = datetime(2026, 9, 21, 10)
    fit = job(req_id="F1", title="Process Excellence Manager", url="https://x/fit/F1")  # no description
    store.record_board(con, "Acme Corp", "workday", [fit], now, track="fit")
    store.record_bridge_board(con, "Acme Corp", "workday", [_bjob("B1")], now,
                              attempted_places={"Springfield, XX"}, place_status={"Springfield, XX": True})
    cands = store.detail_candidates(con, {"workday"}, limit=50)
    ids = {c[0] for c in cands}
    assert store.posting_id("Acme Corp", "workday", "F1", track="fit") in ids
    assert store.posting_id("Acme Corp", "workday", "B1", track="bridge") not in ids


def test_bridge_excluded_from_hard_negatives(tmp_path):
    con = store.connect(str(tmp_path / "t8.duckdb"))
    now = datetime(2026, 9, 21, 10)
    fit_pid, bridge_pid = _seed_fit_and_bridge(con, now)
    for pid in (fit_pid, bridge_pid):
        con.execute("INSERT INTO decisions VALUES (?, 'pass', 'function: wrong lane', 'cli', NULL, ?)",
                    [pid, now])
    rows = {r[0] for r in con.execute("SELECT posting_id FROM vw_hard_negatives").fetchall()}
    assert fit_pid in rows
    assert bridge_pid not in rows


def test_bridge_excluded_from_label_set(tmp_path):
    con = store.connect(str(tmp_path / "t9.duckdb"))
    now = datetime(2026, 9, 21, 10)
    fit_pid, bridge_pid = _seed_fit_and_bridge(con, now)
    fit_hash = con.execute("SELECT description_hash FROM postings WHERE posting_id = ?", [fit_pid]).fetchone()[0]
    bridge_hash = con.execute("SELECT description_hash FROM postings WHERE posting_id = ?", [bridge_pid]).fetchone()[0]
    for pid, h in ((fit_pid, fit_hash), (bridge_pid, bridge_hash)):
        con.execute("""INSERT INTO llm_labels
            (posting_id, description_hash, rubric_version, scorer, grade, grade_process, judged_at)
            VALUES (?, ?, 'r1', 'claude-test', 'bullseye', 'bullseye', ?)""", [pid, h, now])
    for view in ("vw_label_set", "vw_label_set_process"):
        rows = {r[0] for r in con.execute(f"SELECT posting_id FROM {view}").fetchall()}
        assert fit_pid in rows, view
        assert bridge_pid not in rows, view


def test_bridge_excluded_from_lens_fit_even_if_somehow_screened(tmp_path):
    """Defense in depth (sprint plan §31.5): vw_lens_fit filters on p.track = 'fit' directly, not
    only through the INNER JOIN to vw_screen_latest -- so even a bridge posting that somehow
    picked up a `screens` row (never happens through the normal pipeline) is still excluded."""
    con = store.connect(str(tmp_path / "t10.duckdb"))
    now = datetime(2026, 9, 21, 10)
    fit_pid, bridge_pid = _seed_fit_and_bridge(con, now)
    for pid in (fit_pid, bridge_pid):
        con.execute("""INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict,
                       rule_score, final_score, band) VALUES (?, 'rv1', 'none', ?, 'candidate', 50, 50, 'strong')""",
                    [pid, now])
    rows = {r[0] for r in con.execute("SELECT posting_id FROM vw_lens_fit").fetchall()}
    assert fit_pid in rows
    assert bridge_pid not in rows


def test_vw_bridge_open_shows_only_bridge_postings(tmp_path):
    con = store.connect(str(tmp_path / "t11.duckdb"))
    now = datetime(2026, 9, 21, 10)
    fit_pid, bridge_pid = _seed_fit_and_bridge(con, now)
    rows = {r[0] for r in con.execute("SELECT posting_id FROM vw_bridge_open").fetchall()}
    assert bridge_pid in rows
    assert fit_pid not in rows


# ---------------------------------------------------------------- sweep_bridge wiring (§31.8)
def _quiet(*a, **k):
    pass


def test_sweep_bridge_skips_loudly_with_no_places_configured(tmp_path, monkeypatch):
    from backend.ats import sweep as ats_sweep, registry as R
    monkeypatch.setattr(R, "load_bridge_places", lambda max_ring=1, log=None: [])
    con = store.connect(str(tmp_path / "t12.duckdb"))
    rows = [{"employer": "Big Box", "platform": "workday", "identifier_1": "x", "identifier_2": "wd1",
             "identifier_3": "careers", "source": "example", "notes": "", "track": "bridge"}]
    logged = []
    stats = ats_sweep.sweep_bridge(con, rows, workers=1, log=logged.append)
    assert stats["succeeded"] == 0
    assert stats["failed"] == 1
    assert any("no places configured" in line for line in logged)
    assert con.execute("SELECT count(*) FROM postings WHERE track = 'bridge'").fetchone()[0] == 0


def test_sweep_bridge_never_falls_back_to_a_whole_board_pull(tmp_path, monkeypatch):
    """No places configured must never trigger adapters.list_jobs at all -- the guard is
    'skip', never 'pull everything'."""
    from backend.ats import adapters as A
    from backend.ats import sweep as ats_sweep, registry as R
    monkeypatch.setattr(R, "load_bridge_places", lambda max_ring=1, log=None: [])
    called = []
    monkeypatch.setattr(A, "list_jobs", lambda *a, **k: called.append(1) or [])
    con = store.connect(str(tmp_path / "t13.duckdb"))
    rows = [{"employer": "Big Box", "platform": "workday", "identifier_1": "x", "identifier_2": "wd1",
             "identifier_3": "careers", "source": "example", "notes": "", "track": "bridge"}]
    ats_sweep.sweep_bridge(con, rows, workers=1, log=_quiet)
    assert not called


def test_sweep_bridge_pulls_places_and_records_via_record_bridge_board(tmp_path, monkeypatch):
    from backend.ats import adapters as A
    from backend.ats import sweep as ats_sweep, registry as R

    places = [{"place": "Springfield, XX", "ring": 1, "search_text": "Springfield, XX", "state": "XX"}]
    monkeypatch.setattr(R, "load_bridge_places", lambda max_ring=1, log=None: places)

    def fake_list_jobs(row, max_pages=None, scope=None):
        assert scope["strategy"] == "places"
        p = job(req_id="B1", title="Cashier", url="https://x/B1", location_primary="Springfield, XX")
        p["_bridge_places"] = ["Springfield, XX"]
        return A.PlacesPulled([p], {"Springfield, XX": True})

    monkeypatch.setattr(A, "list_jobs", fake_list_jobs)
    con = store.connect(str(tmp_path / "t14.duckdb"))
    rows = [{"employer": "Big Box", "platform": "workday", "identifier_1": "x", "identifier_2": "wd1",
             "identifier_3": "careers", "source": "example", "notes": "", "track": "bridge"}]
    stats = ats_sweep.sweep_bridge(con, rows, workers=1, log=_quiet)
    assert stats["succeeded"] == 1
    assert stats["new"] == 1
    rows_out = con.execute("SELECT employer, track, bridge_place, status FROM postings").fetchall()
    assert rows_out == [("Big Box", "bridge", "Springfield, XX", "active")]
    health = con.execute("SELECT track, ok FROM vw_board_health WHERE employer = 'Big Box'").fetchall()
    assert health == [("bridge", True)]


# ---------------------------------------------------------------- finder.py bridge output (§31.6)
def test_cmd_bridge_sorts_ring_then_voice_low_first_high_last_then_employer_pref(tmp_path, monkeypatch, capsys):
    import finder
    from argparse import Namespace
    from backend.ats import registry as R

    places = [
        {"place": "Springfield, XX", "ring": 1, "search_text": "Springfield, XX", "state": "XX", "evergreen": False},
        {"place": "Rivertown, YY", "ring": 2, "search_text": "Rivertown, YY", "state": "YY", "evergreen": False},
    ]
    monkeypatch.setattr(R, "load_bridge_places", lambda max_ring=1, log=None: places)
    monkeypatch.setattr(R, "load_bridge_employer_order", lambda: {"zeta co": 0, "acme co": 1})

    con = store.connect(str(tmp_path / "t15.duckdb"))
    now = datetime(2026, 9, 21, 10)
    high = _bjob("H1", title="Cashier")
    low = _bjob("L1", title="Overnight Stocker")
    none_ = _bjob("N1", title="Assistant Manager")
    store.record_bridge_board(con, "Acme Co", "workday", [high, low, none_], now,
                              attempted_places={"Springfield, XX"}, place_status={"Springfield, XX": True})
    ring2_job = _bjob("R2", title="Greeter", places=["Rivertown, YY"])
    store.record_bridge_board(con, "Zeta Co", "workday", [ring2_job], now,
                              attempted_places={"Rivertown, YY"}, place_status={"Rivertown, YY": True})

    capsys.readouterr()
    finder.cmd_bridge(con, Namespace(max_ring=2, new_only=False, hide_voice_high=False, include_stale=False))
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines()[1:] if l.strip()]
    titles_in_order = [next(t for t in ("Overnight Stocker", "Assistant Manager", "Cashier", "Greeter") if t in l)
                        for l in lines]
    # ring 1 (Acme's three) before ring 2 (Zeta's Greeter); within ring 1, low before blank before high.
    assert titles_in_order == ["Overnight Stocker", "Assistant Manager", "Cashier", "Greeter"]


def test_cmd_bridge_hide_voice_high_is_off_by_default(tmp_path, monkeypatch, capsys):
    import finder
    from argparse import Namespace
    from backend.ats import registry as R
    monkeypatch.setattr(R, "load_bridge_places",
                        lambda max_ring=1, log=None: [{"place": "Springfield, XX", "ring": 1,
                                            "search_text": "Springfield, XX", "state": "XX", "evergreen": False}])
    monkeypatch.setattr(R, "load_bridge_employer_order", lambda: {})
    con = store.connect(str(tmp_path / "t16.duckdb"))
    now = datetime(2026, 9, 21, 10)
    store.record_bridge_board(con, "Acme Co", "workday", [_bjob("H1", title="Cashier")], now,
                              attempted_places={"Springfield, XX"}, place_status={"Springfield, XX": True})
    capsys.readouterr()
    finder.cmd_bridge(con, Namespace(max_ring=1, new_only=False, hide_voice_high=False, include_stale=False))
    assert "Cashier" in capsys.readouterr().out
    finder.cmd_bridge(con, Namespace(max_ring=1, new_only=False, hide_voice_high=True, include_stale=False))
    assert "Cashier" not in capsys.readouterr().out


# ---------------------------------------------------------------- schema v22 (additive)
def test_v22_migrated_schema_matches_fresh_schema(tmp_path):
    """A fresh v22 DB and a v21 DB migrated up to v22 must end up with the identical set of
    columns on every table `_add_missing_columns` touches (sprint plan §31.5's schema instruction)."""
    fresh = store.connect(str(tmp_path / "fresh.duckdb"))
    fresh_cols = {t: sorted(store._columns(fresh, t)) for t in ("postings", "board_runs")}
    fresh.close()

    migrated_path = str(tmp_path / "migrated.duckdb")
    con = store.connect(migrated_path)
    con.execute("ALTER TABLE postings DROP COLUMN track")
    con.execute("ALTER TABLE postings DROP COLUMN bridge_place")
    con.execute("ALTER TABLE board_runs DROP COLUMN track")
    con.execute("UPDATE schema_info SET version = 21")
    con.close()

    con2 = store.connect(migrated_path)  # re-connect: _add_missing_columns adds them back
    migrated_cols = {t: sorted(store._columns(con2, t)) for t in ("postings", "board_runs")}
    con2.close()

    assert migrated_cols == fresh_cols


# ---------------------------------------------------------------- max_ring (any positive int) + evergreen pools
def test_load_bridge_places_honors_arbitrary_ring_numbers(tmp_path, monkeypatch):
    from backend.ats import registry as R
    csv_text = ("place,ring,search_text,state,evergreen\n"
                '"Springfield, XX",1,"Springfield, XX",XX,\n'
                '"Lakeside, YY",2,"Lakeside, YY",YY,\n'
                '"Hilltown, ZZ",3,"Hilltown, ZZ",ZZ,\n')
    p = tmp_path / "bridge_places.csv"
    p.write_text(csv_text)
    monkeypatch.setattr(R, "BRIDGE_PLACES_CSV", str(p))
    assert [r["place"] for r in R.load_bridge_places(max_ring=1)] == ["Springfield, XX"]
    assert [r["place"] for r in R.load_bridge_places(max_ring=2)] == ["Springfield, XX", "Lakeside, YY"]
    assert [r["place"] for r in R.load_bridge_places(max_ring=3)] == \
        ["Springfield, XX", "Lakeside, YY", "Hilltown, ZZ"]
    assert [r["place"] for r in R.load_bridge_places(max_ring=None)] == \
        ["Springfield, XX", "Lakeside, YY", "Hilltown, ZZ"]


def test_cmd_bridge_max_ring_beyond_two_includes_ring_three(tmp_path, monkeypatch, capsys):
    import finder
    from argparse import Namespace
    from backend.ats import registry as R
    places = [{"place": "Hilltown, ZZ", "ring": 3, "search_text": "Hilltown, ZZ", "state": "ZZ", "evergreen": False}]
    monkeypatch.setattr(R, "load_bridge_places", lambda max_ring=1, log=None: places)
    monkeypatch.setattr(R, "load_bridge_employer_order", lambda: {})
    con = store.connect(str(tmp_path / "t17.duckdb"))
    now = datetime(2026, 9, 21, 10)
    store.record_bridge_board(con, "Acme Co", "workday",
                              [_bjob("H1", title="Greeter", places=["Hilltown, ZZ"])], now,
                              attempted_places={"Hilltown, ZZ"}, place_status={"Hilltown, ZZ": True})
    capsys.readouterr()
    finder.cmd_bridge(con, Namespace(max_ring=2, new_only=False, hide_voice_high=False, include_stale=False))
    assert "Greeter" not in capsys.readouterr().out
    finder.cmd_bridge(con, Namespace(max_ring=3, new_only=False, hide_voice_high=False, include_stale=False))
    assert "Greeter" in capsys.readouterr().out


def test_cmd_bridge_evergreen_place_prints_pool_and_is_never_new(tmp_path, monkeypatch, capsys):
    """A standing application pool (evergreen=true) prints 'pool' instead of days-open and is
    never marked NEW -- even across a run boundary where an ordinary posting WOULD be marked
    NEW -- pure display logic, no heuristic."""
    import finder
    from argparse import Namespace
    from backend.ats import registry as R
    places = [{"place": "Remote", "ring": 1, "search_text": "Remote", "state": "remote", "evergreen": True}]
    monkeypatch.setattr(R, "load_bridge_places", lambda max_ring=1, log=None: places)
    monkeypatch.setattr(R, "load_bridge_employer_order", lambda: {})
    con = store.connect(str(tmp_path / "t18.duckdb"))
    t1 = datetime(2026, 9, 20, 10)
    t2 = datetime(2026, 9, 21, 10)
    # A prior (unrelated) bridge run establishes a "previous run" cutoff for the NEW mark.
    store.record_bridge_board(con, "Other Co", "workday", [_bjob("O1")], t1,
                              attempted_places={"Springfield, XX"}, place_status={"Springfield, XX": True})
    pool_job = job(req_id="P1", title="Any Position", url="https://x/P1", location_primary="Remote")
    pool_job["_bridge_places"] = ["Remote"]
    store.record_bridge_board(con, "Acme Co", "workday", [pool_job], t2,
                              attempted_places={"Remote"}, place_status={"Remote": True})
    capsys.readouterr()
    finder.cmd_bridge(con, Namespace(max_ring=1, new_only=False, hide_voice_high=False, include_stale=False))
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if "Any Position" in l)
    assert "pool" in line
    assert not line.startswith("NEW")


def test_cmd_bridge_non_evergreen_place_still_shows_days_and_new(tmp_path, monkeypatch, capsys):
    import finder
    from argparse import Namespace
    from backend.ats import registry as R
    places = [{"place": "Springfield, XX", "ring": 1, "search_text": "Springfield, XX", "state": "XX",
              "evergreen": False}]
    monkeypatch.setattr(R, "load_bridge_places", lambda max_ring=1, log=None: places)
    monkeypatch.setattr(R, "load_bridge_employer_order", lambda: {})
    con = store.connect(str(tmp_path / "t19.duckdb"))
    now = datetime(2026, 9, 21, 10)
    store.record_bridge_board(con, "Acme Co", "workday", [_bjob("H1", title="Cashier")], now,
                              attempted_places={"Springfield, XX"}, place_status={"Springfield, XX": True})
    capsys.readouterr()
    finder.cmd_bridge(con, Namespace(max_ring=1, new_only=False, hide_voice_high=False, include_stale=False))
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if "Cashier" in l)
    assert "pool" not in line


# ---------------------------------------------------------------- letter A: places-only platforms
def test_list_jobs_refuses_a_places_scope_on_a_platform_with_no_places_adapter():
    """Orchestrator audit 2026-09-21, letter A.1: the old
    `"scope" in inspect.signature(fn).parameters` guard silently let a places-strategy scope fall
    through into an ordinary whole-board call on a platform (e.g. Greenhouse) whose list function
    has no `scope` parameter at all. It must now raise instead of ever calling the whole-board fn."""
    from backend.ats import adapters as A

    row = {"employer": "Big Box", "platform": "greenhouse", "identifier_1": "bigbox"}
    scope = {"strategy": "places", "places": [{"place": "Springfield, XX", "ring": 1,
             "search_text": "Springfield, XX", "state": "XX"}]}
    try:
        A.list_jobs(row, scope=scope)
        assert False, "expected a ValueError, not a silent whole-board pull"
    except ValueError as e:
        assert "places-scoped adapter" in str(e)


def test_list_jobs_places_scope_still_dispatches_on_a_real_places_platform(monkeypatch):
    from backend.ats import adapters as A

    called = []
    monkeypatch.setattr(A, "_workday_places_jobs", lambda row, places, max_pages=None: called.append(places) or [])
    row = {"employer": "Big Box", "platform": "workday", "identifier_1": "x", "identifier_2": "wd1",
           "identifier_3": "careers"}
    scope = {"strategy": "places", "places": [{"place": "Springfield, XX"}]}
    A.list_jobs(row, scope=scope)
    assert called == [[{"place": "Springfield, XX"}]]


def test_sweep_bridge_skips_a_row_on_an_unsupported_platform_loudly_before_any_request(tmp_path, monkeypatch):
    """Orchestrator audit 2026-09-21, letter A.2: `sweep_bridge` itself must never even ask
    `adapters.list_jobs` to pull a bridge-track row on a platform outside PLACES_PLATFORMS."""
    from backend.ats import adapters as A
    from backend.ats import sweep as ats_sweep, registry as R

    places = [{"place": "Springfield, XX", "ring": 1, "search_text": "Springfield, XX", "state": "XX"}]
    monkeypatch.setattr(R, "load_bridge_places", lambda max_ring=1, log=None: places)
    called = []
    monkeypatch.setattr(A, "list_jobs", lambda *a, **k: called.append(1) or [])
    con = store.connect(str(tmp_path / "t20.duckdb"))
    rows = [{"employer": "Small Shop", "platform": "greenhouse", "identifier_1": "smallshop",
             "identifier_2": "", "identifier_3": "", "source": "example", "notes": "", "track": "bridge"}]
    logged = []
    stats = ats_sweep.sweep_bridge(con, rows, workers=1, log=logged.append)
    assert not called, "an unsupported platform must never reach adapters.list_jobs"
    assert stats["failed"] == 1 and stats["succeeded"] == 0
    assert any("no places-scoped adapter" in line for line in logged)
    health = con.execute("SELECT ok, error FROM vw_board_health WHERE employer = 'Small Shop'").fetchall()
    assert health and health[0][0] is False


def test_record_bridge_board_refuses_an_untagged_job(tmp_path):
    """Orchestrator audit 2026-09-21, letter A.3: a job dict with no `_bridge_places` tag must
    never be written as a bridge row -- it can't be told apart from a genuinely place-scoped
    posting by the close pass."""
    con = store.connect(str(tmp_path / "t21.duckdb"))
    now = datetime(2026, 9, 21, 10)
    untagged = job(req_id="U1", title="Cashier", url="https://x/U1", location_primary="Springfield, XX")
    try:
        store.record_bridge_board(con, "Big Box", "workday", [untagged], now,
                                  attempted_places={"Springfield, XX"}, place_status={"Springfield, XX": True})
        assert False, "expected a ValueError for an untagged bridge job"
    except ValueError as e:
        assert "_bridge_places" in str(e)
    assert con.execute("SELECT count(*) FROM postings").fetchone()[0] == 0


# ---------------------------------------------------------------- letter B: registry CSV reading
def test_read_bridge_csv_rows_skips_comment_lines_and_handles_bom(tmp_path, monkeypatch):
    from backend.ats import registry as R

    p = tmp_path / "bridge_places.csv"
    p.write_bytes(b"\xef\xbb\xbfplace,ring,search_text,state\n"
                  b"# a comment row, never a place\n"
                  b'"Springfield, XX",1,"Springfield, XX",XX\n')
    monkeypatch.setattr(R, "BRIDGE_PLACES_CSV", str(p))
    rows = R.load_bridge_places(max_ring=1)
    assert [r["place"] for r in rows] == ["Springfield, XX"]


def test_load_bridge_places_logs_header_mismatch_not_generic_no_places(tmp_path, monkeypatch):
    """Orchestrator audit 2026-09-21, letter B: a file with rows but no usable 'place' column
    (a misspelled header) gets its OWN diagnostic, not the generic 'no places configured' line."""
    from backend.ats import registry as R

    p = tmp_path / "bridge_places.csv"
    p.write_text("plaice,ring,search_text,state\n\"Springfield, XX\",1,\"Springfield, XX\",XX\n")
    monkeypatch.setattr(R, "BRIDGE_PLACES_CSV", str(p))
    logged = []
    rows = R.load_bridge_places(max_ring=1, log=logged.append)
    assert rows == []
    assert any("header" in line.lower() and "place" in line.lower() for line in logged)


# ---------------------------------------------------------------- letter C: location_matches shapes
LOCATION_CASES_FULL_STATE = [
    ("Springfield, Ohio", "Springfield", "OH", True),
    ("SPRINGFIELD OHIO", "Springfield", "OH", True),
    ("Ohio - Springfield", "Springfield", "OH", True),
    ("springfield, ohio", "Springfield", "OH", True),           # case-insensitive
    ("Prince Springfield, Ohio", "Springfield", "OH", False),   # longer town, full-state form
    ("PRINCE SPRINGFIELD OHIO", "Springfield", "OH", False),    # longer town, all-caps form
    ("Ohio - Prince Springfield", "Springfield", "OH", False),  # longer town, dash form
    ("Springfield, Texas", "Springfield", "OH", False),         # same city, wrong (full-name) state
    ("XX - Springfield", "Springfield", "XX", True),            # one-dash form, letter C.1
    ("Springfield, XX", "Prince Springfield", "XX", False),
]


def test_location_matches_full_state_name_and_one_dash_shapes():
    for text, city, state, expected in LOCATION_CASES_FULL_STATE:
        assert bridge.location_matches(text, city, state) is expected, (text, city, state)


def test_location_matches_allow_no_state_city_only():
    # Off by default: a bare city with no state/store-code shape never matches.
    assert bridge.location_matches("Springfield (0350)", "Springfield", "OH") is False
    # Opted in: matches the exact city, still rejects the longer-town case.
    assert bridge.location_matches("Springfield (0350)", "Springfield", "OH", allow_no_state=True) is True
    assert bridge.location_matches("Prince Springfield (0350)", "Springfield", "OH", allow_no_state=True) is False


def test_location_matches_remote_synonyms():
    for text in ("Virtual", "Work From Home", "Work-From-Home", "Work at Home", "WFH",
                 "Telecommute", "Anywhere in the US", "Fully Remote"):
        assert bridge.location_matches(text, "Remote", "remote") is True, text
    assert bridge.location_matches("Springfield, XX", "Remote", "remote") is False


def test_location_matches_fields_seam():
    assert bridge.location_matches_fields("Springfield", "XX", "Springfield", "XX") is True
    assert bridge.location_matches_fields("Prince Springfield", "XX", "Springfield", "XX") is False
    assert bridge.location_matches_fields("Springfield", "YY", "Springfield", "XX") is False


def test_is_multi_site_text():
    assert bridge.is_multi_site_text("2 Locations") is True
    assert bridge.is_multi_site_text("1 Location") is True
    assert bridge.is_multi_site_text("Springfield, XX") is False


def test_workday_places_jobs_logs_multi_site_count_separately_from_dropped(monkeypatch):
    """Orchestrator audit 2026-09-21, letter C.5: a multi-site facet string is counted on its OWN
    number in the per-place log line, distinct from an ordinary non-matching drop."""
    from backend.ats import adapters as A

    def fake_pull(c, url, public, applied, page_cap, search_text=""):
        return [
            job(req_id="R1", title="Cashier", url="https://x/R1", location_primary="Springfield, XX"),
            job(req_id="R2", title="Greeter", url="https://x/R2", location_primary="Rivertown, YY"),
            job(req_id="R3", title="Stocker", url="https://x/R3", location_primary="2 Locations"),
            job(req_id="R4", title="Stocker", url="https://x/R4", location_primary="3 Locations"),
        ], False

    monkeypatch.setattr(A, "_workday_pull", fake_pull)
    monkeypatch.setattr(A, "client", lambda: _FakeClient())
    row = {"employer": "Big Box", "identifier_1": "x", "identifier_2": "wd1", "identifier_3": "careers"}
    places = [{"place": "Springfield, XX", "ring": 1, "search_text": "Springfield, XX", "state": "XX"}]
    logged = []
    A._workday_places_jobs(row, places, log=logged.append)
    line = next(l for l in logged if "Springfield, XX" in l)
    assert "dropped 1 result" in line
    assert "2 multi-site result" in line


class _FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ---------------------------------------------------------------- letter F: track='fit' isolation
def test_pseudo_negatives_never_includes_a_bridge_posting_with_description_text(tmp_path):
    from backend.finder import labels

    con = store.connect(str(tmp_path / "t22.duckdb"))
    now = datetime(2026, 9, 21, 10)
    br = _bjob("B1", title="Overnight Stocker")
    br["description_text"] = _LONG_DESC
    store.record_bridge_board(con, "Acme Corp", "workday", [br], now,
                              attempted_places={"Springfield, XX"}, place_status={"Springfield, XX": True})
    bridge_pid = store.posting_id("Acme Corp", "workday", "B1", track="bridge")
    docs = labels.pseudo_negatives(con, n=50)
    assert bridge_pid not in {d.posting_id for d in docs}


def test_posting_index_never_matches_a_bridge_row(tmp_path):
    from backend.finder.labels import PostingIndex

    con = store.connect(str(tmp_path / "t23.duckdb"))
    now = datetime(2026, 9, 21, 10)
    store.record_bridge_board(con, "Acme Corp", "workday", [_bjob("B1", title="Cashier")], now,
                              attempted_places={"Springfield, XX"}, place_status={"Springfield, XX": True})
    idx = PostingIndex(con)
    pid, kind = idx.match_kind("Acme Corp", "Cashier", url="https://x/B1")
    assert pid is None


# ---------------------------------------------------------------- letter G: NEW cutoff by sweep run_id
def test_cmd_bridge_new_cutoff_uses_the_previous_sweeps_run_id_not_per_board_ran_at(tmp_path, monkeypatch, capsys):
    """Orchestrator audit 2026-09-21, letter G: two boards in the SAME sweep (run_id) finish at
    interleaved microsecond timestamps -- the old `DISTINCT ran_at` cutoff could pick a timestamp
    from the middle of the CURRENT sweep as the 'previous run', wrongly hiding the NEW mark on
    rows from that same sweep. The cutoff must be the previous sweep's run_id START, not any
    individual board's ran_at."""
    import finder
    from argparse import Namespace
    from backend.ats import registry as R

    places = [{"place": "Springfield, XX", "ring": 1, "search_text": "Springfield, XX", "state": "XX",
              "evergreen": False}]
    monkeypatch.setattr(R, "load_bridge_places", lambda max_ring=1, log=None: places)
    monkeypatch.setattr(R, "load_bridge_employer_order", lambda: {})
    con = store.connect(str(tmp_path / "t24.duckdb"))

    # Sweep 1 ("run-a"): two boards, interleaved microsecond ran_at.
    t_a1 = datetime(2026, 9, 20, 10, 0, 0, 100000)
    t_a2 = datetime(2026, 9, 20, 10, 0, 0, 300000)
    store.record_bridge_board(con, "Acme Co", "workday", [_bjob("OLD1")], t_a1,
                              attempted_places={"Springfield, XX"}, place_status={"Springfield, XX": True})
    store.log_board(con, "run-a", "Acme Co", "workday", True, 1.0, t_a1, track="bridge")
    store.log_board(con, "run-a", "Zeta Co", "workday", True, 1.0, t_a2, track="bridge")

    # Sweep 2 ("run-b"): also two boards, interleaved -- one of them (t_b1) is EARLIER than
    # run-a's t_a2, so a naive DISTINCT-ran_at-ordered cutoff could pick a run-a timestamp as if
    # it were run-b's own start, or vice versa. The correct cutoff is min(ran_at) of run-a, since
    # run-a is the sweep before run-b (the most recent).
    t_b1 = datetime(2026, 9, 20, 10, 0, 0, 200000)
    t_b2 = datetime(2026, 9, 21, 10, 0, 0, 0)
    new_job = _bjob("NEW1")
    store.record_bridge_board(con, "Acme Co", "workday", [_bjob("OLD1"), new_job], t_b2,
                              attempted_places={"Springfield, XX"}, place_status={"Springfield, XX": True})
    store.log_board(con, "run-b", "Acme Co", "workday", True, 1.0, t_b1, track="bridge")
    store.log_board(con, "run-b", "Zeta Co", "workday", True, 1.0, t_b2, track="bridge")

    capsys.readouterr()
    finder.cmd_bridge(con, Namespace(max_ring=1, new_only=False, hide_voice_high=False, include_stale=False))
    out = capsys.readouterr().out
    new1_line = next(l for l in out.splitlines() if "R2" in l or new_job["req_id"] in l or "NEW1" in l)
    old1_line = next(l for l in out.splitlines() if "OLD1" in l)
    assert new1_line.startswith("NEW")
    assert not old1_line.startswith("NEW")


def test_cmd_bridge_does_not_mark_the_previous_sweeps_later_boards_new(tmp_path, monkeypatch, capsys):
    """Re-audit 2026-09-21: a posting first seen by the SECOND board of the previous sweep sits after
    that sweep's start, so a cutoff at the previous sweep's start kept printing it NEW one sweep
    later. NEW is only what the latest sweep saw for the first time."""
    import finder
    from argparse import Namespace
    from backend.ats import registry as R

    places = [{"place": "Springfield, XX", "ring": 1, "search_text": "Springfield, XX", "state": "XX",
              "evergreen": False}]
    monkeypatch.setattr(R, "load_bridge_places", lambda max_ring=1, log=None: places)
    monkeypatch.setattr(R, "load_bridge_employer_order", lambda: {})
    con = store.connect(str(tmp_path / "t26.duckdb"))
    kw = dict(attempted_places={"Springfield, XX"}, place_status={"Springfield, XX": True})

    a1, a2 = datetime(2026, 9, 20, 10, 0, 0), datetime(2026, 9, 20, 10, 0, 30)
    store.record_bridge_board(con, "Acme Co", "workday", [_bjob("A1", title="Clerk Alpha")], a1, **kw)
    store.log_board(con, "run-a", "Acme Co", "workday", True, 1.0, a1, track="bridge")
    store.record_bridge_board(con, "Zeta Co", "workday", [_bjob("Z1", title="Clerk Zulu")], a2, **kw)
    store.log_board(con, "run-a", "Zeta Co", "workday", True, 1.0, a2, track="bridge")

    b1, b2 = datetime(2026, 9, 21, 10, 0, 0), datetime(2026, 9, 21, 10, 0, 30)
    store.record_bridge_board(con, "Acme Co", "workday", [_bjob("A1", title="Clerk Alpha"), _bjob("A2", title="Clerk Bravo")], b1, **kw)
    store.log_board(con, "run-b", "Acme Co", "workday", True, 1.0, b1, track="bridge")
    store.record_bridge_board(con, "Zeta Co", "workday", [_bjob("Z1", title="Clerk Zulu")], b2, **kw)
    store.log_board(con, "run-b", "Zeta Co", "workday", True, 1.0, b2, track="bridge")

    capsys.readouterr()
    finder.cmd_bridge(con, Namespace(max_ring=1, new_only=False, hide_voice_high=False, include_stale=False))
    lines = capsys.readouterr().out.splitlines()
    assert next(l for l in lines if "Clerk Bravo" in l).startswith("NEW")      # first board of the latest sweep
    assert not next(l for l in lines if "Clerk Zulu" in l).startswith("NEW")  # second board of the PREVIOUS sweep
    assert not next(l for l in lines if "Clerk Alpha" in l).startswith("NEW")


def test_cmd_bridge_marks_nothing_new_on_the_very_first_sweep(tmp_path, monkeypatch, capsys):
    import finder
    from argparse import Namespace
    from backend.ats import registry as R

    places = [{"place": "Springfield, XX", "ring": 1, "search_text": "Springfield, XX", "state": "XX",
              "evergreen": False}]
    monkeypatch.setattr(R, "load_bridge_places", lambda max_ring=1, log=None: places)
    monkeypatch.setattr(R, "load_bridge_employer_order", lambda: {})
    con = store.connect(str(tmp_path / "t25.duckdb"))
    now = datetime(2026, 9, 21, 10)
    store.record_bridge_board(con, "Acme Co", "workday", [_bjob("F1")], now,
                              attempted_places={"Springfield, XX"}, place_status={"Springfield, XX": True})
    store.log_board(con, "run-only", "Acme Co", "workday", True, 1.0, now, track="bridge")
    capsys.readouterr()
    finder.cmd_bridge(con, Namespace(max_ring=1, new_only=False, hide_voice_high=False, include_stale=False))
    line = next(l for l in capsys.readouterr().out.splitlines() if "F1" in l or "Cashier" in l)
    assert not line.startswith("NEW")


# ---------------------------------------------------------------- letter E: staleness
def test_vw_bridge_open_hides_stale_rows_include_stale_shows_them(tmp_path, monkeypatch, capsys):
    import finder
    from argparse import Namespace
    from backend.ats import registry as R
    from backend.ats import bridge as B

    places = [{"place": "Springfield, XX", "ring": 1, "search_text": "Springfield, XX", "state": "XX",
              "evergreen": False}]
    monkeypatch.setattr(R, "load_bridge_places", lambda max_ring=1, log=None: places)
    monkeypatch.setattr(R, "load_bridge_employer_order", lambda: {})
    con = store.connect(str(tmp_path / "t26.duckdb"))
    old = datetime(2026, 9, 1, 10)   # well over BRIDGE_STALE_DAYS before "now"
    store.record_bridge_board(con, "Acme Co", "workday", [_bjob("S1", title="Cashier")], old,
                              attempted_places={"Springfield, XX"}, place_status={"Springfield, XX": True})
    assert B.BRIDGE_STALE_DAYS == 10

    capsys.readouterr()
    finder.cmd_bridge(con, Namespace(max_ring=1, new_only=False, hide_voice_high=False, include_stale=False))
    out = capsys.readouterr().out
    assert "Cashier" not in out
    assert "stale" in out.lower()

    capsys.readouterr()
    finder.cmd_bridge(con, Namespace(max_ring=1, new_only=False, hide_voice_high=False, include_stale=True))
    out2 = capsys.readouterr().out
    assert "Cashier" in out2


# ---------------------------------------------------------------- letter H: voice word boundaries
def test_voice_flag_word_boundaries_and_technical_titles():
    assert bridge.voice_flag("SQL Server Administrator") == ""
    assert bridge.voice_flag("Hostler") == ""
    assert bridge.voice_flag("Server") == "high"
    assert bridge.voice_flag("Host") == "high"
    assert bridge.voice_flag("Hostess") == "high"


# ---------------------------------------------------------------- letter I: --limit after --track
def test_sweep_run_limit_applies_after_track_filter(tmp_path, monkeypatch):
    from backend.ats import sweep as ats_sweep, registry as R

    registry_rows = (
        [{"employer": f"Fit Co {i}", "platform": "greenhouse", "identifier_1": f"fit{i}",
          "identifier_2": "", "identifier_3": "", "source": "example", "notes": "", "track": "fit"}
         for i in range(3)]
        + [{"employer": f"Bridge Co {i}", "platform": "workday", "identifier_1": "x",
            "identifier_2": "wd1", "identifier_3": "careers", "source": "example", "notes": "",
            "track": "bridge"} for i in range(5)]
    )
    monkeypatch.setattr(ats_sweep, "load_registry", lambda: registry_rows)
    monkeypatch.setattr(R, "load_bridge_places", lambda max_ring=1, log=None: [])

    seen_bridge_rows = []

    def fake_sweep_bridge(con, rows, max_ring=1, workers=8, max_pages=None, log=print):
        seen_bridge_rows.extend(rows)
        return dict(run_id="r", started=datetime(2026, 9, 21, 10), elapsed=0.0, attempted=len(rows),
                    succeeded=0, failed=len(rows), seen=0, new=0, reopened=0, closed=0)

    monkeypatch.setattr(ats_sweep, "sweep_bridge", fake_sweep_bridge)
    db_path = str(tmp_path / "t27.duckdb")
    ats_sweep.run(db_path=db_path, track="bridge", limit=2, detail_budget=0, screen=False, log=lambda *a, **k: None)
    # Confirm the limited set was 2 BRIDGE rows, not (as the old order would give) the first 2
    # rows of the combined fit+bridge registry list (which are both `fit`, so sweep_bridge would
    # have been called with zero rows).
    assert len(seen_bridge_rows) == 2
    assert all(r["track"] == "bridge" for r in seen_bridge_rows)
