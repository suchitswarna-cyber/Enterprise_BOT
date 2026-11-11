"""Risk-tiered policy engine.

Every tool declares a risk tier. Before any tool runs, the policy engine decides
whether the call is allowed outright, needs human approval (and how many
approvers), or is denied. The model never sees or bypasses this layer: it lives
in the orchestrator, between the model's tool request and the connector call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum


class Risk(IntEnum):
    READ = 0      # look-ups, diagnostics: no side effects
    LOW = 1       # reversible, small scope (e.g. unlock an account)
    HIGH = 2      # changes access or state for others; reversible
    CRITICAL = 3  # disables accounts, prod-impacting, hard to reverse


class Verdict:
    ALLOW = "allow"
    NEEDS_APPROVAL = "needs_approval"
    DENY = "deny"


@dataclass(frozen=True)
class Requester:
    user_id: str
    role: str  # employee | it_agent | it_admin | system
    incident_mode: bool = False  # a declared P1 incident lifts change freezes


@dataclass(frozen=True)
class Decision:
    verdict: str
    reason: str
    approvals_required: int = 0


@dataclass(frozen=True)
class FreezeWindow:
    start: datetime
    end: datetime
    label: str

    def active(self, now: datetime) -> bool:
        return self.start <= now < self.end


# Highest risk tier each role may request. Above it the call is denied, not escalated.
ROLE_CEILING: dict[str, Risk] = {
    "employee": Risk.LOW,
    "it_agent": Risk.HIGH,
    "it_admin": Risk.CRITICAL,
    "system": Risk.HIGH,  # automated self-healing never performs CRITICAL actions
}

# Approvals needed per tier, before role adjustments.
APPROVALS: dict[Risk, int] = {Risk.READ: 0, Risk.LOW: 0, Risk.HIGH: 1, Risk.CRITICAL: 2}


@dataclass
class PolicyEngine:
    freezes: list[FreezeWindow] = field(default_factory=list)
    # Tools an employee may run on their *own* account (e.g. unlock, password reset).
    self_service_tools: frozenset[str] = frozenset()

    def decide(
        self,
        tool_name: str,
        risk: Risk,
        requester: Requester,
        subject: str | None,
        now: datetime,
    ) -> Decision:
        ceiling = ROLE_CEILING.get(requester.role)
        if ceiling is None:
            return Decision(Verdict.DENY, f"unknown role '{requester.role}'")
        if risk > ceiling:
            return Decision(Verdict.DENY, f"role '{requester.role}' may not request {risk.name} actions")

        if requester.role == "employee":
            # `subject` is the person whose account/device the call touches.
            if subject is not None and subject != requester.user_id:
                return Decision(Verdict.DENY, "employees may only view or act on their own account and devices")
            if risk >= Risk.LOW and tool_name not in self.self_service_tools:
                return Decision(Verdict.DENY, f"'{tool_name}' is not a self-service action")

        if risk >= Risk.HIGH and not requester.incident_mode:
            for w in self.freezes:
                if w.active(now):
                    return Decision(Verdict.DENY, f"change freeze in effect: {w.label}")

        needed = APPROVALS[risk]
        if requester.role == "it_admin" and risk == Risk.HIGH:
            needed = 0  # admins are trusted for single reversible changes
        if needed:
            return Decision(
                Verdict.NEEDS_APPROVAL,
                f"{risk.name} action requires {needed} approver(s)",
                approvals_required=needed,
            )
        return Decision(Verdict.ALLOW, f"{risk.name} action permitted for {requester.role}")


def validate_approvers(approvers: list[str], requester: Requester, required: int) -> str | None:
    """Return an error string if the approver set is insufficient, else None."""
    unique = {a.strip() for a in approvers if a and a.strip()}
    if requester.user_id in unique:
        return "requesters cannot approve their own actions"
    if len(unique) < required:
        return f"need {required} distinct approver(s), got {len(unique)}"
    return None
