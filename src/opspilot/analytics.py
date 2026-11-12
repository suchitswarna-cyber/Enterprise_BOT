"""Operational metrics computed from the audit log (plus current ticket/KB state).

Definitions (also shown on the dashboard):
  * chat turn      - one user message answered by the agent (`chat_turn` event)
  * self-resolved  - a turn that applied at least one fix and opened no ticket
  * deflection     - self-resolved turns / all answered turns (errors excluded)
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from statistics import median

from .connectors import Systems


def read_entries(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, int(q * len(v)))], 1)


def _top(counter: Counter, n: int = 8) -> list[dict]:
    return [{"label": str(k), "value": v} for k, v in counter.most_common(n)]


def compute(entries: list[dict], systems: Systems | None = None) -> dict:
    turns = [e for e in entries if e.get("event") == "chat_turn"]
    answered = [t for t in turns if not t.get("error")]
    durations = [float(t["duration_s"]) for t in answered if "duration_s" in t]
    self_resolved = [t for t in answered if t.get("fixes", 0) > 0 and not t.get("ticket_created")]

    executed = [e for e in entries if e.get("event") == "action_executed"]
    changes = [e for e in executed if e.get("risk") not in (None, "READ")]
    decisions = [e for e in entries if e.get("event") == "policy_decision"]
    denials = [e for e in decisions if e.get("verdict") == "deny"]
    approvals = [e for e in entries if e.get("event") == "approval"]
    failovers = [e for e in entries if e.get("event") == "model_failover"]
    outages = [e for e in executed
               if isinstance(e.get("result"), dict) and (e["result"].get("outage_problem") or {}).get("new_problem")]

    out: dict = {
        "definitions": {
            "self_resolved": "turns that applied at least one fix and opened no ticket",
            "deflection_rate": "self-resolved turns / answered turns",
        },
        "kpis": {
            "sessions": len({e.get("session") for e in entries if e.get("session")}),
            "chat_turns": len(turns),
            "answered_turns": len(answered),
            "failed_turns": len(turns) - len(answered),
            "self_resolved": len(self_resolved),
            "deflection_rate": round(len(self_resolved) / len(answered), 3) if answered else None,
            "median_response_s": round(median(durations), 1) if durations else None,
            "p90_response_s": _pct(durations, 0.9),
            "changes_made": len(changes),
            "denials": len(denials),
            "approvals_granted": sum(1 for a in approvals if a.get("ok")),
            "approvals_rejected": sum(1 for a in approvals if not a.get("ok")),
            "rollbacks": sum(1 for e in entries if e.get("event") == "rollback"),
            "model_failovers": len(failovers),
            "outages_detected": len(outages),
        },
        "actions_by_tool": _top(Counter(e.get("tool") for e in executed)),
        "denials_by_reason": _top(Counter(e.get("reason") for e in denials)),
        "failovers_by_model": _top(Counter(f"{e.get('from_model')} ({e.get('code')})" for e in failovers)),
        "turns_by_model": _top(Counter(t.get("model") for t in answered)),
    }
    if systems is not None:
        tickets = systems.tickets.list()
        out["tickets_by_status"] = _top(Counter(t["status"] for t in tickets))
        out["kpis"]["open_problems"] = sum(1 for t in tickets
                                           if t.get("kind") == "problem" and t["status"] not in ("resolved", "closed"))
        out["kpis"]["kb_drafts_pending"] = len(systems.kb.list("draft"))
    return out
