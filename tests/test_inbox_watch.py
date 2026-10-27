"""Inbox Watch: Graph and Claude faked; no network, no tracker writes."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend import inbox_watch as iw

NOW = datetime(2026, 10, 24, 15, 0, tzinfo=timezone.utc)
ROWS = [
    {"app_id": 497, "company": "Contoso", "role": "Senior People Data Analyst", "req": "400101", "status": "applied"},
    {"app_id": 300, "company": "Northwind Federal Credit Union", "role": "Process Transformation Analyst",
     "req": "500201", "status": "interview"},
    {"app_id": 340, "company": "Northwind Federal Credit Union", "role": "Senior Business Intelligence Analyst",
     "req": "500202", "status": "screen"},
]


def msg(i, frm, subject, preview=""):
    return {"id": f"AAMk{i:04d}xyz", "receivedDateTime": "2026-10-24T13:00:00Z", "subject": subject,
            "bodyPreview": preview, "parentFolderId": "inbox",
            "from": {"emailAddress": {"name": frm.split("@")[0], "address": frm}}}


class FakeGraph:
    def __init__(self, mails, bodies):
        self.mails, self.bodies, self.calls = mails, bodies, []

    def _graph_get(self, path, params=None, headers=None):
        self.calls.append(path)
        if path.startswith("/me/mailFolders/"):
            return {"id": path.rsplit("/", 1)[1]}
        if path == "/me/messages":
            return {"value": self.mails}
        return {"body": {"content": self.bodies[path.rsplit("/", 1)[1]]}}


def fake_runner(answers, calls):
    def run(cmd, **kw):
        calls.append((cmd, kw))
        if cmd[0] == "claude":
            return SimpleNamespace(returncode=0, stdout=json.dumps({"structured_output": {"emails": answers}}),
                                   stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return run


def ans(n, classes, **kw):
    base = {"n": n, "classes": classes, "company": "", "role": "", "req_id": "", "contact_name": "",
            "reason": "r"}
    base.update(kw)
    return base


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(iw, "STATE_DIR", tmp_path / "state")
    (tmp_path / "state").mkdir()
    job = tmp_path / "Job_Search"
    job.mkdir()
    return job


def test_filter_skips_noise_and_self():
    keys = sorted({k for r in ROWS for k in iw.company_keys(r["company"])})
    assert keys == ["contoso", "northwind", "northwindfederalcreditunion"]
    assert iw.is_candidate(msg(1, "no-reply@contoso.com", "Thank you"), keys, "me@x.com")
    assert iw.is_candidate(msg(2, "x@myworkday.com", "Update"), keys, "me@x.com")
    assert iw.is_candidate(msg(3, "pat@acme.com", "Quick question about a role"), keys, "me@x.com")
    assert not iw.is_candidate(msg(4, "deals@shop.com", "50% off shoes"), keys, "me@x.com")
    assert not iw.is_candidate(msg(5, "me@x.com", "Your application"), keys, "me@x.com")


def test_match_by_req_then_company_then_role():
    m = msg(1, "no-reply@contoso.com", "Update")
    assert iw.match_row(ans(1, ["rejected"], req_id="400101"), m, "", ROWS)[0]["app_id"] == 497
    assert iw.match_row(ans(1, ["rejected"], company="Contoso"), m, "", ROWS)[0]["app_id"] == 497
    n = msg(2, "talent@northwindfederal.org", "Next steps")
    row, why = iw.match_row(ans(1, ["screen"], company="Northwind Federal"), n, "", ROWS)
    assert row is None and "2 rows" in why
    row, _ = iw.match_row(ans(1, ["screen"], company="Northwind Federal", role="Process Transformation Analyst"),
                          n, "", ROWS)
    assert row["app_id"] == 300
    assert iw.match_row(ans(1, ["screen"], company="Acme"), msg(3, "a@acme.com", "Hi"), "", ROWS)[0] is None


def test_plan_move_only_forward_and_safe():
    assert iw.plan_move(["rejected"], "applied") == "rejected"
    assert iw.plan_move(["rejected"], "rejected") is None
    assert iw.plan_move(["screen"], "applied") == "screen"
    assert iw.plan_move(["screen"], "assumed-rejected") == "screen"
    assert iw.plan_move(["screen", "conversation-request"], "interview") is None
    assert iw.plan_move(["cold-inbound"], "applied") is None


def test_trial_run_records_without_writing(env):
    mails = [msg(1, "no-reply@contoso.com", "Regarding your application 400101"),
             msg(2, "pat@acme.com", "Opportunity: Data Lead", "Are you free for a call this week?"),
             msg(3, "deals@shop.com", "Sale")]
    g = FakeGraph(mails, {m["id"]: "body" for m in mails})
    calls = []
    answers = [ans(1, ["rejected"], company="Contoso", req_id="400101"),
               ans(2, ["cold-inbound", "conversation-request"], company="Acme", role="Data Lead",
                   contact_name="Pat Lee", proposed_times="Tue 2pm")]
    res = iw.run(False, ol=g, rows=ROWS, runner=fake_runner(answers, calls), now=NOW, job_dir=env)
    assert res == {"error": None, "alerts": 1}
    assert [c[0][0] for c in calls] == ["claude"]                 # no trk call in trial
    cmd, kw = calls[0]
    assert cmd[cmd.index("--tools") + 1] == ""
    assert kw["env"]["CLAUDE_CONFIG_DIR"].endswith("/.claude")
    assert "Sale" not in kw["input"]                               # filtered before Claude
    report = (env / "Inbox_Watch.md").read_text()
    assert "TRIAL" in report and "WOULD SET #497 Contoso" in report and "applied -> rejected" in report
    assert "## NEEDS YOU (1)" in report and "Cold contact: Pat Lee at Acme" in report
    shell = next((env / "Replies").glob("*Acme_Data-Lead.md")).read_text()
    assert "{{option_1_date}}" in shell and "Hi Pat," in shell and "Tue 2pm" in shell


def test_live_run_writes_with_actor_and_does_not_repeat(env):
    mails = [msg(1, "no-reply@contoso.com", "Your application has been sent to the hiring manager")]
    g = FakeGraph(mails, {mails[0]["id"]: "passed initial screening"})
    calls = []
    run = fake_runner([ans(1, ["screen"], company="Contoso")], calls)
    iw.run(True, ol=g, rows=ROWS, runner=run, now=NOW, job_dir=env)
    trk = [c for c in calls if c[0][0] == "trk"]
    assert trk[0][0][:4] == ["trk", "set", "497", "screen"] and trk[0][1]["env"]["TRK_ACTOR"] == "inbox-watch"
    assert "DONE #497 Contoso" in (env / "Inbox_Watch.md").read_text()
    calls.clear()
    iw.run(True, ol=g, rows=ROWS, runner=run, now=NOW, job_dir=env)   # same mail again: already seen
    assert calls == []


def test_clear_by_deleting_line_and_by_command(env):
    mails = [msg(1, "a@acme.com", "Call?", "can we talk"), msg(2, "b@beta.com", "Role", "chat?")]
    g = FakeGraph(mails, {m["id"]: "x" for m in mails})
    answers = [ans(1, ["cold-inbound"], company="Acme"), ans(2, ["cold-inbound"], company="Beta")]
    iw.run(False, ol=g, rows=ROWS, runner=fake_runner(answers, []), now=NOW, job_dir=env)
    report = env / "Inbox_Watch.md"
    lines = report.read_text().split("\n")
    acme = next(l for l in lines if "Acme" in l and "iw:" in l)
    report.write_text("\n".join(l for l in lines if l != acme))
    beta_id = iw.ALERT_RE.findall(report.read_text())[0]
    assert iw.clear(beta_id, job_dir=env)
    g.mails = []
    iw.run(False, ol=g, rows=ROWS, runner=fake_runner([], []), now=NOW, job_dir=env)
    text = report.read_text()
    assert "## NEEDS YOU (0)" in text and "Nothing waiting." in text


def test_failure_is_reported_and_window_retried(env):
    class Boom(FakeGraph):
        def _graph_get(self, *a, **k):
            raise RuntimeError("Sign-in expired")
    res = iw.run(False, ol=Boom([], {}), rows=ROWS, now=NOW, job_dir=env)
    assert "Sign-in expired" in res["error"]
    assert "Last run failed" in (env / "Inbox_Watch.md").read_text()
    assert iw.load_state(iw.STATE_DIR / "state.json")["last_success"] is None


def test_match_handles_real_tracker_names():
    rows = [{"app_id": 273, "company": "Fabrikam Health", "role": "Senior Business Consultant (Remote)",
             "req": "61234-1A", "status": "applied"},
            {"app_id": 272, "company": "T&R Bank / Riverside Trust", "role": "Sr. Business Process Improvement",
             "req": "", "status": "applied"}]
    cf = msg(1, "careers-noreply@fabrikam.com", "Thank you for your interest in Job ID:61234 - Senior Business Consultant")
    assert iw.match_row(ans(1, ["rejected"]), cf, "", rows)[0]["app_id"] == 273
    mt = msg(2, "trb@myworkday.com", "T&R Bank Application")
    assert iw.match_row(ans(1, ["rejected"], company="T&R Bank"), mt, "", rows)[0]["app_id"] == 272


def test_one_alert_per_topic_and_no_cold_alert_for_tracked_row(env):
    mails = [msg(1, "hit-reply@linkedin.com", "Career Opportunity: Architect", "interested?"),
             msg(2, "hit-reply@linkedin.com", "Career Opportunity: Architect", "following up"),
             msg(3, "talent@contoso.com", "You've been referred for the role", "apply here")]
    g = FakeGraph(mails, {m["id"]: "x" for m in mails})
    answers = [ans(1, ["cold-inbound"], company="Tailspin", role="Architect"),
               ans(2, ["cold-inbound"], company="Tailspin", role="Architect"),
               ans(3, ["cold-inbound"], company="Contoso", role="Senior People Data Analyst")]
    res = iw.run(False, ol=g, rows=ROWS, runner=fake_runner(answers, []), now=NOW, job_dir=env)
    assert res["alerts"] == 1
    text = (env / "Inbox_Watch.md").read_text()
    assert "Tailspin" in text.split("## What")[0] and "Contoso" not in text.split("## What")[0]


def test_role_word_in_subject_is_not_a_company():
    rows = [{"app_id": 293, "company": "Forward Contoso", "role": "Program Manager", "req": "",
             "status": "applied"}]
    m = msg(1, "inmail-hit-reply@linkedin.com", "AI Transformation Leader/Forward Deployed Engineer- Tailspin")
    row, why = iw.match_row(ans(1, ["cold-inbound"], company="Tailspin"), m, "", rows)
    assert row is None and why == "no tracker row for this company"
    # the full company name in the subject still counts
    m2 = msg(2, "inmail-hit-reply@linkedin.com", "Forward Contoso: next steps")
    assert iw.match_row(ans(2, ["screen"]), m2, "", rows)[0]["app_id"] == 293


def test_tie_prefers_the_single_open_row():
    rows = [{"app_id": 1, "company": "Acme", "role": "Analyst", "req": "R12345", "status": "rejected"},
            {"app_id": 2, "company": "Acme", "role": "Analyst", "req": "R12345", "status": "applied"}]
    m = msg(1, "jobs@acme.com", "Update on R12345")
    assert iw.match_row(ans(1, ["screen"], req_id="R12345"), m, "", rows)[0]["app_id"] == 2
    rows[1]["status"] = "rejected"
    assert iw.match_row(ans(1, ["screen"], req_id="R12345"), m, "", rows)[0] is None
