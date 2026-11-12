"""Tool registry.

Each tool declares:
  * a JSON schema the model fills in,
  * a risk tier, either fixed or computed from the arguments (context-aware risk:
    adding someone to `vpn-users` is HIGH, adding them to `it-admins` is CRITICAL),
  * a `subject` resolver naming whose account/device the call touches,
  * a `preview` shown to approvers before anything runs (dry-run plan),
  * a handler returning the result and, for mutations, the inverse step.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .connectors import RUNBOOKS, Systems
from .policy import Risk
from .spikes import check_spike

PRIVILEGED_GROUPS = {"it-admins", "prod-db-readers"}


@dataclass
class Outcome:
    result: dict
    undo: tuple[str, dict, str] | None = None  # (tool, input, description)


@dataclass
class Tool:
    name: str
    description: str
    properties: dict[str, dict]
    risk: Risk | Callable[[dict], Risk]
    handler: Callable[[Systems, dict], Outcome]
    subject: Callable[[Systems, dict], str | None] = lambda s, a: a.get("user_id")
    preview: Callable[[dict], str] | None = None
    # False = only callable from the UI/API (e.g. human KB review), never offered to the model.
    model_visible: bool = True

    def risk_for(self, args: dict) -> Risk:
        return self.risk(args) if callable(self.risk) else self.risk

    def plan(self, args: dict) -> str:
        if self.preview:
            return self.preview(args)
        return f"{self.name}({', '.join(f'{k}={v!r}' for k, v in args.items())})"

    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": self.properties, "required": list(self.properties)}


def _s(desc: str, enum: list[str] | None = None) -> dict:
    d: dict[str, Any] = {"type": "string", "description": desc}
    if enum:
        d["enum"] = enum
    return d


def _device_owner(s: Systems, a: dict) -> str | None:
    d = s.endpoints.devices.get(a.get("device_id", ""))
    return d["owner"] if d else None


def _ticket_owner(s: Systems, a: dict) -> str | None:
    t = s.tickets.tickets.get(a.get("ticket_id", ""))
    return t["requester"] if t else None


# ------------------------------------------------------------------- handlers
def _unlock(s: Systems, a: dict) -> Outcome:
    u = s.directory.get_user(a["user_id"])
    if not u["locked"]:
        return Outcome({"status": "already_unlocked"})
    s.directory.set_locked(a["user_id"], False)
    return Outcome({"status": "unlocked"}, ("lock_account", {"user_id": a["user_id"]}, "re-lock account"))


def _lock(s: Systems, a: dict) -> Outcome:
    s.directory.set_locked(a["user_id"], True)
    return Outcome({"status": "locked"}, ("unlock_account", {"user_id": a["user_id"]}, "unlock account"))


def _reset_pw(s: Systems, a: dict) -> Outcome:
    receipt = s.directory.reset_password(a["user_id"])
    return Outcome({"status": "temporary password delivered out-of-band to the user's registered device",
                    "receipt": receipt})


def _group_change(add: bool):
    def h(s: Systems, a: dict) -> Outcome:
        fn = s.directory.add_to_group if add else s.directory.remove_from_group
        changed = fn(a["user_id"], a["group"])
        if not changed:
            return Outcome({"status": "no_change"})
        inverse = "remove_user_from_group" if add else "add_user_to_group"
        verb = "remove from" if add else "re-add to"
        return Outcome({"status": "added" if add else "removed", "group": a["group"]},
                       (inverse, dict(a), f"{verb} {a['group']}"))
    return h


def _set_enabled(enabled: bool):
    def h(s: Systems, a: dict) -> Outcome:
        s.directory.set_enabled(a["user_id"], enabled)
        inverse = "disable_account" if enabled else "enable_account"
        return Outcome({"status": "enabled" if enabled else "disabled"},
                       (inverse, {"user_id": a["user_id"], "reason": "rollback"}, f"undo {'enable' if enabled else 'disable'}"))
    return h


def _runbook(s: Systems, a: dict) -> Outcome:
    msg = s.endpoints.run(a["device_id"], a["runbook"])
    inv = RUNBOOKS[a["runbook"]]["inverse"]
    undo = ("run_runbook", {"device_id": a["device_id"], "runbook": inv}, f"run {inv}") if inv else None
    return Outcome({"status": msg, "health_after": s.endpoints.health(a["device_id"])["findings"]}, undo)


def _create_ticket(s: Systems, a: dict) -> Outcome:
    dupes = s.tickets.find_similar(a["title"])
    tid = s.tickets.create(a["title"], a["description"], a["priority"], a["user_id"])
    result = {"ticket_id": tid, "possible_duplicates": dupes}
    spike = check_spike(s.tickets, tid, datetime.now(timezone.utc))
    if spike:
        result["outage_problem"] = spike
        result["next_step"] = (f"this looks like part of a wider outage tracked as {spike['problem_id']}; "
                               "tell the user IT is already on it")
    return Outcome(result,
                   ("set_ticket_status", {"ticket_id": tid, "status": "closed", "note": "cancelled (rollback)"},
                    f"cancel {tid}"))


def _set_status(s: Systems, a: dict) -> Outcome:
    before = s.tickets.get(a["ticket_id"])["status"]
    s.tickets.set_status(a["ticket_id"], a["status"])
    if a["note"]:
        s.tickets.add_note(a["ticket_id"], a["note"])
    return Outcome({"ticket_id": a["ticket_id"], "status": a["status"]},
                   ("set_ticket_status", {"ticket_id": a["ticket_id"], "status": before, "note": "reverted"},
                    f"revert to {before}"))


def _resolve(s: Systems, a: dict) -> Outcome:
    """Knowledge flywheel: every resolution drafts a KB article for human review."""
    t = s.tickets.get(a["ticket_id"])
    s.tickets.set_status(a["ticket_id"], "resolved", a["resolution"])
    kb = s.kb.draft(f"How to fix: {t['title']}", a["resolution"], a["ticket_id"])
    return Outcome({"ticket_id": a["ticket_id"], "status": "resolved", "kb_draft": kb},
                   ("set_ticket_status", {"ticket_id": a["ticket_id"], "status": t["status"], "note": "reopened"},
                    "reopen ticket"))


def _review_kb(s: Systems, a: dict) -> Outcome:
    return Outcome(s.kb.review(a["kb_id"], a["decision"] == "publish", a["note"]))


# -------------------------------------------------------------------- registry
def build_tools() -> dict[str, Tool]:
    uid = _s("Directory user id, e.g. 'alice'")
    tools = [
        # READ -----------------------------------------------------------------
        Tool("lookup_user", "Get a user's directory profile: lock state, MFA, groups, manager.",
             {"user_id": uid}, Risk.READ, lambda s, a: Outcome(s.directory.get_user(a["user_id"]))),
        Tool("list_user_devices", "List device ids owned by a user.",
             {"user_id": uid}, Risk.READ,
             lambda s, a: Outcome({"devices": s.endpoints.devices_for(a["user_id"])})),
        Tool("check_device_health", "Diagnose a device: disk, CPU, patch age, firewall, service states, findings.",
             {"device_id": _s("Device id, e.g. 'LT-ALICE-01'")}, Risk.READ,
             lambda s, a: Outcome(s.endpoints.health(a["device_id"])), subject=_device_owner),
        Tool("list_active_alerts", "List active monitoring alerts across the fleet.",
             {"scope": _s("Always 'all'", ["all"])}, Risk.READ,
             lambda s, a: Outcome({"alerts": s.monitoring.active()}), subject=lambda s, a: None),
        Tool("search_kb", "Search the knowledge base for known fixes. Do this before troubleshooting from scratch.",
             {"query": _s("Symptoms in plain words")}, Risk.READ,
             lambda s, a: Outcome({"articles": s.kb.search(a["query"])}), subject=lambda s, a: None),
        Tool("find_similar_tickets", "Find open tickets similar to a description (incident clustering).",
             {"text": _s("Issue summary")}, Risk.READ,
             lambda s, a: Outcome({"matches": s.tickets.find_similar(a["text"])}), subject=lambda s, a: None),
        Tool("get_ticket", "Read a ticket.", {"ticket_id": _s("e.g. 'INC-1001'")}, Risk.READ,
             lambda s, a: Outcome(s.tickets.get(a["ticket_id"])), subject=_ticket_owner),
        # LOW (self-service eligible) ------------------------------------------
        Tool("unlock_account", "Unlock a locked directory account.", {"user_id": uid}, Risk.LOW, _unlock),
        Tool("lock_account", "Lock a directory account (e.g. suspected compromise).", {"user_id": uid},
             Risk.HIGH, _lock),
        Tool("reset_password", "Issue a temporary password, delivered out-of-band to the user's registered device. "
             "The password is never shown to you.", {"user_id": uid}, Risk.LOW, _reset_pw),
        Tool("create_ticket", "Open a ticket. Returns possible duplicates; link instead of re-opening when one matches.",
             {"user_id": _s("Requester user id"), "title": _s("Short title"),
              "description": _s("What happened, what was tried"),
              "priority": _s("P1 outage .. P4 request", ["P1", "P2", "P3", "P4"])},
             Risk.LOW, _create_ticket),
        Tool("run_runbook", "Run an approved remediation from the runbook catalog on a device. "
             "Catalog: " + "; ".join(f"{k}: {v['desc']}" for k, v in RUNBOOKS.items()),
             {"device_id": _s("Device id"), "runbook": _s("Runbook name", list(RUNBOOKS))},
             lambda a: Risk.HIGH if a.get("runbook") == "disable_firewall" else Risk.LOW,
             _runbook, subject=_device_owner,
             preview=lambda a: f"Run '{a['runbook']}' on {a['device_id']}: {RUNBOOKS.get(a['runbook'], {}).get('desc', '?')}"),
        Tool("set_ticket_status", "Change a ticket's status and add a note.",
             {"ticket_id": _s("Ticket id"), "status": _s("New status", ["open", "in_progress", "resolved", "closed"]),
              "note": _s("Work note; empty string for none")},
             Risk.LOW, _set_status, subject=_ticket_owner),
        Tool("resolve_ticket", "Resolve a ticket with the fix applied. Automatically drafts a KB article for review.",
             {"ticket_id": _s("Ticket id"), "resolution": _s("Root cause and the exact fix steps")},
             Risk.LOW, _resolve, subject=_ticket_owner),
        # HIGH / CRITICAL -------------------------------------------------------
        Tool("add_user_to_group", "Grant group membership (access). Privileged groups are CRITICAL.",
             {"user_id": uid, "group": _s("Group name")},
             lambda a: Risk.CRITICAL if a.get("group") in PRIVILEGED_GROUPS else Risk.HIGH,
             _group_change(True),
             preview=lambda a: f"GRANT {a['user_id']} -> {a['group']}"
             + (" (PRIVILEGED)" if a.get("group") in PRIVILEGED_GROUPS else "")),
        Tool("remove_user_from_group", "Revoke group membership.", {"user_id": uid, "group": _s("Group name")},
             Risk.HIGH, _group_change(False), preview=lambda a: f"REVOKE {a['user_id']} from {a['group']}"),
        Tool("enable_account", "Re-enable a disabled account.", {"user_id": uid, "reason": _s("Justification")},
             Risk.HIGH, _set_enabled(True)),
        Tool("disable_account", "Disable an account entirely (offboarding, confirmed compromise).",
             {"user_id": uid, "reason": _s("Justification")}, Risk.CRITICAL, _set_enabled(False),
             preview=lambda a: f"DISABLE account {a['user_id']} — reason: {a['reason']}"),
        # Human-only ------------------------------------------------------------
        # Publishing a KB draft is a human review step: the model drafted it, so the
        # model must not be the one approving it. Not self-service, so IT staff only.
        Tool("review_kb_article", "Publish or reject a drafted KB article.",
             {"kb_id": _s("Article id"), "decision": _s("Review decision", ["publish", "reject"]),
              "note": _s("Reviewer note; empty string for none")},
             Risk.LOW, _review_kb, subject=lambda s, a: None, model_visible=False),
    ]
    return {t.name: t for t in tools}


SELF_SERVICE = frozenset({"unlock_account", "reset_password", "create_ticket", "run_runbook",
                          "set_ticket_status", "resolve_ticket"})
