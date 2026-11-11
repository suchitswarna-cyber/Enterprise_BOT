from datetime import datetime, timedelta, timezone

import pytest

from opspilot.audit import AuditLog
from opspilot.orchestrator import Orchestrator
from opspilot.policy import FreezeWindow, PolicyEngine, Requester
from opspilot.tools import SELF_SERVICE

NOW = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)


def make(tmp_path, user="alice", role="employee", approver=None, **kw):
    o = Orchestrator(Requester(user, role, kw.pop("incident", False)), AuditLog(tmp_path / "audit.jsonl"),
                     clock=lambda: NOW, **kw)
    if approver:
        o.approver = approver
    return o


def test_employee_can_unlock_own_account(tmp_path):
    o = make(tmp_path)
    r = o.execute("unlock_account", {"user_id": "alice"})
    assert r["ok"] and r["status"] == "unlocked"
    assert o.systems.directory.users["alice"]["locked"] is False


def test_employee_cannot_touch_someone_else(tmp_path):
    o = make(tmp_path)
    assert o.execute("reset_password", {"user_id": "bob"})["error"] == "denied_by_policy"
    assert o.execute("lookup_user", {"user_id": "bob"})["error"] == "denied_by_policy"
    assert o.execute("check_device_health", {"device_id": "LT-BOB-07"})["error"] == "denied_by_policy"


def test_employee_cannot_grant_access_even_to_self(tmp_path):
    o = make(tmp_path)
    r = o.execute("add_user_to_group", {"user_id": "alice", "group": "eng-users"})
    assert r["error"] == "denied_by_policy"


def test_password_never_returned(tmp_path):
    o = make(tmp_path)
    r = o.execute("reset_password", {"user_id": "alice"})
    assert r["ok"] and "password" not in {k.lower() for k in r}
    assert o.systems.directory.notifications  # delivered out of band


def test_high_risk_needs_one_approver_not_self(tmp_path):
    o = make(tmp_path, "bob", "it_agent", approver=lambda p, r, n: ["bob"])
    r = o.execute("add_user_to_group", {"user_id": "alice", "group": "eng-users"})
    assert r["error"] == "not_approved" and "own" in r["reason"]

    o.approver = lambda p, r, n: ["dana"]
    assert o.execute("add_user_to_group", {"user_id": "alice", "group": "eng-users"})["ok"]


def test_context_aware_risk_privileged_group_is_critical(tmp_path):
    # it_agent ceiling is HIGH, so a privileged grant is denied outright.
    o = make(tmp_path, "bob", "it_agent", approver=lambda p, r, n: ["dana"])
    r = o.execute("add_user_to_group", {"user_id": "alice", "group": "it-admins"})
    assert r["error"] == "denied_by_policy"

    seen = {}
    def approver(plan, risk, n):
        seen.update(plan=plan, n=n)
        return ["carol", "erin"][:n]
    admin = make(tmp_path, "dana", "it_admin", approver=approver)
    assert admin.execute("add_user_to_group", {"user_id": "alice", "group": "it-admins"})["ok"]
    assert seen["n"] == 2 and "PRIVILEGED" in seen["plan"]


def test_undo_reverses_last_change(tmp_path):
    o = make(tmp_path, "dana", "it_admin")
    o.execute("add_user_to_group", {"user_id": "bob", "group": "vpn-users"})
    assert "vpn-users" in o.systems.directory.users["bob"]["groups"]
    r = o.undo_last()
    assert r["ok"] and "vpn-users" not in o.systems.directory.users["bob"]["groups"]
    assert o.undo_last()["error"] == "nothing to undo"


def test_change_freeze_blocks_high_risk_unless_incident(tmp_path):
    freeze = FreezeWindow(NOW - timedelta(hours=1), NOW + timedelta(hours=1), "quarter-end close")
    policy = PolicyEngine([freeze], SELF_SERVICE)
    o = make(tmp_path, "dana", "it_admin", policy=policy)
    r = o.execute("remove_user_from_group", {"user_id": "alice", "group": "vpn-users"})
    assert r["error"] == "denied_by_policy" and "freeze" in r["reason"]
    # LOW actions continue during a freeze.
    assert o.execute("unlock_account", {"user_id": "alice"})["ok"]

    inc = make(tmp_path, "dana", "it_admin", policy=policy, incident=True)
    assert inc.execute("remove_user_from_group", {"user_id": "alice", "group": "vpn-users"})["ok"]


def test_blast_radius_limit(tmp_path):
    o = make(tmp_path, "dana", "it_admin", max_mutations=2)
    o.execute("lock_account", {"user_id": "bob"})
    o.execute("unlock_account", {"user_id": "bob"})
    assert o.execute("lock_account", {"user_id": "bob"})["error"] == "blast_radius_limit"
    assert o.execute("lookup_user", {"user_id": "bob"})["user_id"] == "bob"  # reads still fine


def test_runbook_risk_depends_on_runbook(tmp_path):
    o = make(tmp_path)  # alice owns LT-ALICE-01
    r = o.execute("run_runbook", {"device_id": "LT-ALICE-01", "runbook": "clear_temp_files"})
    assert r["ok"] and "low disk space" not in r["health_after"]
    r = o.execute("run_runbook", {"device_id": "LT-ALICE-01", "runbook": "disable_firewall"})
    assert r["error"] == "denied_by_policy"


def test_resolution_drafts_kb_article(tmp_path):
    o = make(tmp_path, "bob")
    r = o.execute("resolve_ticket", {"ticket_id": "INC-1001", "resolution": "Restarted VPN client service."})
    assert r["kb_draft"] in o.systems.kb.articles
    assert o.systems.kb.articles[r["kb_draft"]]["status"] == "draft"


def test_create_ticket_flags_duplicates(tmp_path):
    o = make(tmp_path, "alice")
    r = o.execute("create_ticket", {"user_id": "alice", "title": "VPN disconnects constantly",
                                    "description": "drops", "priority": "P3"})
    assert any(d["ticket_id"] == "INC-1001" for d in r["possible_duplicates"])


@pytest.mark.parametrize("args,msg", [
    ({}, "missing"),
    ({"user_id": 5}, "string"),
    ({"user_id": "alice", "extra": "x"}, "unexpected"),
])
def test_argument_validation(tmp_path, args, msg):
    assert msg in make(tmp_path).execute("unlock_account", args)["error"]


def test_every_decision_is_audited_and_chain_verifies(tmp_path):
    o = make(tmp_path)
    o.execute("unlock_account", {"user_id": "alice"})
    o.execute("reset_password", {"user_id": "bob"})
    ok, _ = o.audit.verify()
    events = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert ok and len(events) == 3  # 2 decisions + 1 execution
