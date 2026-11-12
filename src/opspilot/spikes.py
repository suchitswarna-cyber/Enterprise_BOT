"""Outage spike detection (incident clustering).

When N similar incidents are opened within M minutes, that is probably one outage
rather than N separate problems. The first time the threshold is crossed we open a
single P2 *problem* ticket and link the incidents to it; later similar incidents
are linked to the same problem instead of being worked one by one.

Similarity is the ticketing connector's `find_similar` (token overlap in the mock,
embeddings in production).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from .connectors import MockTicketing

SPIKE_MIN_TICKETS = int(os.environ.get("OPSPILOT_SPIKE_MIN_TICKETS", 3))
SPIKE_WINDOW_MIN = float(os.environ.get("OPSPILOT_SPIKE_WINDOW_MIN", 15))


def _created(t: dict) -> datetime | None:
    ts = t.get("created_at")
    return datetime.fromisoformat(ts) if ts else None


def check_spike(tickets: MockTicketing, ticket_id: str, now: datetime,
                min_tickets: int = SPIKE_MIN_TICKETS, window_min: float = SPIKE_WINDOW_MIN) -> dict | None:
    """Call after creating `ticket_id`. Returns what happened, or None if no spike."""
    new = tickets.tickets[ticket_id]
    if new.get("kind") == "problem":
        return None
    similar = [m["ticket_id"] for m in tickets.find_similar(new["title"])]

    # Already part of a known outage? Link to the open problem ticket.
    for tid in similar:
        t = tickets.tickets[tid]
        prb = tid if t.get("kind") == "problem" else t.get("problem")
        if prb and prb != ticket_id and tickets.tickets[prb]["status"] not in ("resolved", "closed"):
            tickets.link_to_problem(ticket_id, prb)
            return {"problem_id": prb, "new_problem": False, "linked": len(tickets.tickets[prb]["linked"])}

    cutoff = now - timedelta(minutes=window_min)
    cluster = [tid for tid in similar
               if tickets.tickets[tid].get("kind", "incident") == "incident"
               and (c := _created(tickets.tickets[tid])) is not None and c >= cutoff]
    if ticket_id not in cluster or len(cluster) < min_tickets:
        return None

    prb = tickets.create(
        title=f"Possible outage: {new['title']}",
        description=(f"{len(cluster)} similar incidents opened within {window_min:g} minutes: "
                     + ", ".join(sorted(cluster))),
        priority="P2", requester="opspilot", kind="problem", now=now)
    for tid in cluster:
        tickets.link_to_problem(tid, prb)
    return {"problem_id": prb, "new_problem": True, "linked": len(cluster)}
