"""Orchestrator: the only path from a model's tool request to a real system.

  model tool call -> validate args -> policy decision -> (human approval)
                  -> blast-radius check -> connector -> audit -> rollback journal

Kept independent of any LLM so it can be tested and reused by other front ends
(chat, Slack/Teams bot, self-healing loop, API).
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .audit import AuditLog
from .connectors import Systems
from .policy import Decision, PolicyEngine, Requester, Risk, Verdict, validate_approvers
from .rollback import RollbackJournal, UndoStep
from .tools import SELF_SERVICE, Tool, build_tools

# approver(plan, risk, approvals_required) -> approver user ids ([] = rejected)
Approver = Callable[[str, Risk, int], list[str]]


def reject_all(plan: str, risk: Risk, required: int) -> list[str]:
    return []


@dataclass
class Orchestrator:
    requester: Requester
    audit: AuditLog
    systems: Systems = field(default_factory=Systems)
    policy: PolicyEngine = field(default_factory=lambda: PolicyEngine(self_service_tools=SELF_SERVICE))
    approver: Approver = reject_all
    max_mutations: int = 10  # blast-radius limiter per session
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    tools: dict[str, Tool] = field(default_factory=build_tools)
    journal: RollbackJournal = field(default_factory=RollbackJournal)
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    mutations: int = 0
    # Serialises tool execution. Pass one shared RLock when several sessions share `systems`.
    lock: Any = field(default_factory=threading.RLock)

    # ------------------------------------------------------------------ helpers
    def _validate(self, tool: Tool, args: dict) -> str | None:
        for key, spec in tool.properties.items():
            if key not in args:
                return f"missing argument '{key}'"
            if not isinstance(args[key], str):
                return f"argument '{key}' must be a string"
            if "enum" in spec and args[key] not in spec["enum"]:
                return f"argument '{key}' must be one of {spec['enum']}"
        extra = set(args) - set(tool.properties)
        if extra:
            return f"unexpected argument(s) {sorted(extra)}"
        return None

    def _log(self, event: str, **fields) -> None:
        self.audit.record(event, session=self.session_id, requester=self.requester.user_id,
                          role=self.requester.role, **fields)

    # ------------------------------------------------------------------ execute
    def execute(self, name: str, args: dict, *, is_undo: bool = False) -> dict:
        with self.lock:
            return self._execute(name, args, is_undo=is_undo)

    def _execute(self, name: str, args: dict, *, is_undo: bool = False) -> dict:
        tool = self.tools.get(name)
        if tool is None:
            return {"error": f"unknown tool '{name}'"}
        err = self._validate(tool, args)
        if err:
            return {"error": err}

        risk = tool.risk_for(args)
        subject = tool.subject(self.systems, args)
        decision: Decision = self.policy.decide(name, risk, self.requester, subject, self.clock())
        self._log("policy_decision", tool=name, args=args, risk=risk.name,
                  verdict=decision.verdict, reason=decision.reason, undo=is_undo)

        if decision.verdict == Verdict.DENY:
            return {"error": "denied_by_policy", "reason": decision.reason,
                    "next_step": "explain this to the user and offer to open a ticket for IT staff"}

        approvers: list[str] = []
        if decision.verdict == Verdict.NEEDS_APPROVAL:
            plan = tool.plan(args)
            approvers = self.approver(plan, risk, decision.approvals_required)
            problem = validate_approvers(approvers, self.requester, decision.approvals_required)
            self._log("approval", tool=name, plan=plan, approvers=approvers, ok=problem is None, problem=problem)
            if problem:
                return {"error": "not_approved", "reason": problem}

        if risk > Risk.READ:
            if self.mutations >= self.max_mutations:
                self._log("blast_radius_block", tool=name, mutations=self.mutations)
                return {"error": "blast_radius_limit",
                        "reason": f"session reached {self.max_mutations} changes; hand off to a human"}

        try:
            outcome = tool.handler(self.systems, args)
        except (KeyError, ValueError) as e:
            self._log("action_failed", tool=name, args=args, error=str(e))
            return {"error": "action_failed", "reason": str(e).strip("'\"")}

        action_id = uuid.uuid4().hex[:8]
        if risk > Risk.READ:
            self.mutations += 1
        self._log("action_executed", action_id=action_id, tool=name, args=args, risk=risk.name,
                  approvers=approvers, result=outcome.result)

        if outcome.undo and not is_undo:
            tool_name, tool_input, desc = outcome.undo
            self.journal.push(UndoStep(action_id, desc, tool_name, tool_input))
        return {"ok": True, "action_id": action_id, **outcome.result}

    def undo_last(self) -> dict:
        step = self.journal.peek()
        if step is None:
            return {"error": "nothing to undo"}
        result = self.execute(step.tool, step.tool_input, is_undo=True)
        if result.get("ok"):
            self.journal.pop()
            self._log("rollback", undone_action=step.action_id, description=step.description)
        return {"undone": step.description, **result}
