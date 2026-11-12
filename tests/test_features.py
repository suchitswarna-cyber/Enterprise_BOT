"""Outage spike detection, KB review, dashboard metrics and the streaming web API."""

import json
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from google.genai import types

from opspilot import analytics, web
from opspilot.audit import AuditLog
from opspilot.connectors import MockTicketing, Systems
from opspilot.orchestrator import Orchestrator
from opspilot.policy import Requester
from opspilot.spikes import check_spike

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------- spikes
def _open(t: MockTicketing, title: str, at: datetime, who="alice") -> tuple[str, dict | None]:
    tid = t.create(title, "", "P3", who, now=at)
    return tid, check_spike(t, tid, at, min_tickets=3, window_min=15)


def test_three_similar_tickets_in_window_open_one_problem():
    t = MockTicketing()
    _, s1 = _open(t, "Printer on floor 3 is offline", NOW)
    _, s2 = _open(t, "Printer floor 3 offline again", NOW + timedelta(minutes=2), "bob")
    tid3, s3 = _open(t, "Floor 3 printer offline", NOW + timedelta(minutes=4), "dana")
    assert s1 is None and s2 is None
    assert s3["new_problem"] and s3["linked"] == 3
    prb = t.tickets[s3["problem_id"]]
    assert prb["kind"] == "problem" and prb["priority"] == "P2" and tid3 in prb["linked"]

    # A fourth similar ticket joins the existing problem instead of opening another.
    tid4, s4 = _open(t, "printer offline on floor 3", NOW + timedelta(minutes=30))
    assert s4 == {"problem_id": s3["problem_id"], "new_problem": False, "linked": 4}
    assert t.tickets[tid4]["problem"] == s3["problem_id"]
    assert sum(1 for x in t.tickets.values() if x.get("kind") == "problem") == 1


def test_similar_tickets_spread_out_are_not_an_outage():
    t = MockTicketing()
    _open(t, "Printer on floor 3 is offline", NOW)
    _open(t, "Printer floor 3 offline again", NOW + timedelta(minutes=20))
    _, s = _open(t, "Floor 3 printer offline", NOW + timedelta(minutes=40))
    assert s is None


def test_create_ticket_tool_reports_outage(tmp_path):
    o = Orchestrator(Requester("dana", "it_agent"), AuditLog(tmp_path / "a.jsonl"))
    results = [o.execute("create_ticket", {"user_id": "dana", "title": f"Office wifi down in building B ({i})",
                                           "description": "", "priority": "P3"}) for i in range(3)]
    assert "outage_problem" not in results[0]
    assert results[2]["outage_problem"]["new_problem"] is True


# ---------------------------------------------------------------- KB review
def _resolved_draft(o: Orchestrator) -> str:
    r = o.execute("resolve_ticket", {"ticket_id": "INC-1001", "resolution": "Pinned the VPN MTU to 1400."})
    return r["kb_draft"]


def test_kb_drafts_need_staff_review_before_search(tmp_path):
    staff = Orchestrator(Requester("dana", "it_agent"), AuditLog(tmp_path / "a.jsonl"))
    kb_id = _resolved_draft(staff)
    assert all(a["kb_id"] != kb_id for a in staff.systems.kb.search("VPN MTU"))  # draft is not searchable

    employee = Orchestrator(Requester("bob", "employee"), AuditLog(tmp_path / "a.jsonl"), systems=staff.systems)
    denied = employee.execute("review_kb_article", {"kb_id": kb_id, "decision": "publish", "note": ""})
    assert denied["error"] == "denied_by_policy"

    ok = staff.execute("review_kb_article", {"kb_id": kb_id, "decision": "publish", "note": "looks right"})
    assert ok["ok"] and ok["status"] == "published"
    assert any(a["kb_id"] == kb_id for a in staff.systems.kb.search("VPN MTU"))
    again = staff.execute("review_kb_article", {"kb_id": kb_id, "decision": "reject", "note": ""})
    assert again["error"] == "action_failed"


# ---------------------------------------------------------------- analytics
def test_dashboard_metrics_from_audit(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    log.record("chat_turn", session="s1", duration_s=4.0, tools=3, fixes=1, ticket_created=False, error=False, model="m1")
    log.record("chat_turn", session="s1", duration_s=10.0, tools=2, fixes=0, ticket_created=True, error=False, model="m1")
    log.record("chat_turn", session="s2", duration_s=30.0, tools=0, fixes=0, ticket_created=False, error=True, model="m2")
    log.record("policy_decision", session="s2", tool="reset_password", verdict="deny", reason="not yours")
    log.record("action_executed", session="s1", tool="unlock_account", risk="LOW", result={"status": "unlocked"})
    log.record("action_executed", session="s1", tool="lookup_user", risk="READ", result={})
    log.record("model_failover", session="s2", from_model="m2", to_model="m1", code=429)
    s = analytics.compute(analytics.read_entries(log.path), Systems())
    k = s["kpis"]
    assert (k["chat_turns"], k["answered_turns"], k["failed_turns"], k["self_resolved"]) == (3, 2, 1, 1)
    assert k["deflection_rate"] == 0.5 and k["median_response_s"] == 7.0
    assert k["changes_made"] == 1 and k["denials"] == 1 and k["model_failovers"] == 1 and k["sessions"] == 2
    assert s["denials_by_reason"] == [{"label": "not yours", "value": 1}]
    assert {r["label"] for r in s["tickets_by_status"]} == {"open"}


# ------------------------------------------------------------- web streaming
class _FakeModels:
    def __init__(self, script):
        self.script = list(script)

    def generate_content(self, model, contents, config):
        return self.script.pop(0)


def _reply(*parts):
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=list(parts)), finish_reason=types.FinishReason.STOP)])


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-used")
    monkeypatch.setattr(web, "AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    web.SYSTEMS.__init__()
    web.SESSIONS.clear()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    web.SYSTEMS.__init__()
    web.SESSIONS.clear()


def _post(base, path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=10)


def test_stream_sends_progress_then_reply_and_records_turn(server):
    sid = json.load(_post(server, "/api/session", {"user": "alice", "role": "employee"}))["session_id"]
    web.SESSIONS[sid].agent.client = SimpleNamespace(models=_FakeModels([
        _reply(types.Part(function_call=types.FunctionCall(id="c1", name="unlock_account", args={"user_id": "alice"}))),
        _reply(types.Part(text="Unlocked.")),
    ]))
    events = [json.loads(line) for line in _post(server, "/api/chat/stream",
                                                 {"session_id": sid, "message": "locked out"}).read().splitlines()]
    kinds = [e["type"] for e in events]
    assert kinds == ["start", "thinking", "tool", "thinking", "done"]
    assert events[2]["tool"] == "unlock_account" and events[2]["result"]["ok"]
    assert events[-1]["reply"] == "Unlocked." and events[-1]["undo_available"]

    stats = json.load(urllib.request.urlopen(server + "/api/stats"))
    assert stats["kpis"]["chat_turns"] == 1 and stats["kpis"]["self_resolved"] == 1

    tickets = json.load(urllib.request.urlopen(f"{server}/api/tickets?session_id={sid}"))
    assert tickets["scope"] == "mine" and {t["requester"] for t in tickets["tickets"]} == {"alice"}


def test_kb_review_endpoint_enforces_policy(server):
    emp = json.load(_post(server, "/api/session", {"user": "bob", "role": "employee"}))["session_id"]
    staff = json.load(_post(server, "/api/session", {"user": "dana", "role": "it_agent"}))["session_id"]
    kb_id = web.SYSTEMS.kb.draft("How to fix: x", "steps", "INC-1001")
    with pytest.raises(urllib.error.HTTPError) as e:
        _post(server, "/api/kb/review", {"session_id": emp, "kb_id": kb_id, "decision": "publish"})
    assert e.value.code == 403
    assert json.load(_post(server, "/api/kb/review", {"session_id": staff, "kb_id": kb_id, "decision": "publish"}))["ok"]
    kb = json.load(urllib.request.urlopen(f"{server}/api/kb?session_id={emp}"))
    assert kb["can_review"] is False and kb_id in {a["kb_id"] for a in kb["articles"]}
