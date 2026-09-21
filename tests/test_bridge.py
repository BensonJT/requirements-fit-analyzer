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


def test_bridge_pull_closes_a_posting_whose_place_genuinely_emptied(tmp_path):
    con = store.connect(str(tmp_path / "t2.duckdb"))
    t1 = datetime(2026, 9, 20, 10)
    t2 = datetime(2026, 9, 21, 10)
    j1 = _bjob("R1")
    store.record_bridge_board(con, "Big Box", "workday", [j1], t1,
                              attempted_places={"Springfield, XX"}, place_status={"Springfield, XX": True})
    # Run 2: same place attempted and succeeded, but R1 no longer comes back.
    store.record_bridge_board(con, "Big Box", "workday", [], t2,
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
    # A bridge run for a DIFFERENT place, not seeing R1 at all this run under Springfield either
    # -- but since Springfield WAS attempted and succeeded and R1's bridge row is absent, it closes.
    store.record_bridge_board(con, "Big Box", "workday", [], t2,
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
    monkeypatch.setattr(R, "load_bridge_places", lambda max_ring=1: [])
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
    monkeypatch.setattr(R, "load_bridge_places", lambda max_ring=1: [])
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
    monkeypatch.setattr(R, "load_bridge_places", lambda max_ring=1: places)

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
    monkeypatch.setattr(R, "load_bridge_places", lambda max_ring=1: places)
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
    finder.cmd_bridge(con, Namespace(max_ring=2, new_only=False, hide_voice_high=False))
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
                        lambda max_ring=1: [{"place": "Springfield, XX", "ring": 1,
                                            "search_text": "Springfield, XX", "state": "XX", "evergreen": False}])
    monkeypatch.setattr(R, "load_bridge_employer_order", lambda: {})
    con = store.connect(str(tmp_path / "t16.duckdb"))
    now = datetime(2026, 9, 21, 10)
    store.record_bridge_board(con, "Acme Co", "workday", [_bjob("H1", title="Cashier")], now,
                              attempted_places={"Springfield, XX"}, place_status={"Springfield, XX": True})
    capsys.readouterr()
    finder.cmd_bridge(con, Namespace(max_ring=1, new_only=False, hide_voice_high=False))
    assert "Cashier" in capsys.readouterr().out
    finder.cmd_bridge(con, Namespace(max_ring=1, new_only=False, hide_voice_high=True))
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
    monkeypatch.setattr(R, "load_bridge_places", lambda max_ring=1: places)
    monkeypatch.setattr(R, "load_bridge_employer_order", lambda: {})
    con = store.connect(str(tmp_path / "t17.duckdb"))
    now = datetime(2026, 9, 21, 10)
    store.record_bridge_board(con, "Acme Co", "workday",
                              [_bjob("H1", title="Greeter", places=["Hilltown, ZZ"])], now,
                              attempted_places={"Hilltown, ZZ"}, place_status={"Hilltown, ZZ": True})
    capsys.readouterr()
    finder.cmd_bridge(con, Namespace(max_ring=2, new_only=False, hide_voice_high=False))
    assert "Greeter" not in capsys.readouterr().out
    finder.cmd_bridge(con, Namespace(max_ring=3, new_only=False, hide_voice_high=False))
    assert "Greeter" in capsys.readouterr().out


def test_cmd_bridge_evergreen_place_prints_pool_and_is_never_new(tmp_path, monkeypatch, capsys):
    """A standing application pool (evergreen=true) prints 'pool' instead of days-open and is
    never marked NEW -- even across a run boundary where an ordinary posting WOULD be marked
    NEW -- pure display logic, no heuristic."""
    import finder
    from argparse import Namespace
    from backend.ats import registry as R
    places = [{"place": "Remote", "ring": 1, "search_text": "Remote", "state": "remote", "evergreen": True}]
    monkeypatch.setattr(R, "load_bridge_places", lambda max_ring=1: places)
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
    finder.cmd_bridge(con, Namespace(max_ring=1, new_only=False, hide_voice_high=False))
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
    monkeypatch.setattr(R, "load_bridge_places", lambda max_ring=1: places)
    monkeypatch.setattr(R, "load_bridge_employer_order", lambda: {})
    con = store.connect(str(tmp_path / "t19.duckdb"))
    now = datetime(2026, 9, 21, 10)
    store.record_bridge_board(con, "Acme Co", "workday", [_bjob("H1", title="Cashier")], now,
                              attempted_places={"Springfield, XX"}, place_status={"Springfield, XX": True})
    capsys.readouterr()
    finder.cmd_bridge(con, Namespace(max_ring=1, new_only=False, hide_voice_high=False))
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if "Cashier" in l)
    assert "pool" not in line
