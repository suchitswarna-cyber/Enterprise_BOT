"""Command line front end.

  opspilot chat --user alice --role employee     # interactive help-desk chat
  opspilot heal                                  # self-healing pass over active alerts
  opspilot audit-verify                          # check the audit hash chain
"""

from __future__ import annotations

import argparse
import os
import sys

from .audit import AuditLog
from .orchestrator import Orchestrator
from .policy import Requester, Risk

AUDIT_PATH = os.environ.get("OPSPILOT_AUDIT_LOG", "var/audit.jsonl")


def console_approver(plan: str, risk: Risk, required: int) -> list[str]:
    print(f"\n  APPROVAL NEEDED [{risk.name}] {plan}")
    print(f"  Enter {required} approver id(s), comma-separated, or blank to reject.")
    raw = input("  approvers> ").strip()
    return [a.strip() for a in raw.split(",") if a.strip()]


def _show_tool(name: str, args: dict, result: dict) -> None:
    status = "ok" if result.get("ok") else result.get("error", "?")
    print(f"  · {name}({', '.join(f'{k}={v}' for k, v in args.items())}) -> {status}")


def cmd_chat(a: argparse.Namespace) -> int:
    from .agent import GeminiAgent

    orch = Orchestrator(Requester(a.user, a.role, a.incident), AuditLog(AUDIT_PATH), approver=console_approver)
    agent = GeminiAgent(orch, model=a.model)
    print(f"OpsPilot ({agent.model}) — signed in as {a.user} [{a.role}]. Commands: /undo /quit")
    while True:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if not text:
            continue
        if text == "/quit":
            return 0
        if text == "/undo":
            print(orch.undo_last())
            continue
        print("\nopspilot>", agent.ask(text, on_tool=_show_tool))


def cmd_heal(a: argparse.Namespace) -> int:
    from .agent import GeminiAgent

    orch = Orchestrator(Requester("opspilot-healer", "system"), AuditLog(AUDIT_PATH), approver=console_approver)
    agent = GeminiAgent(orch, model=a.model)
    for alert in orch.systems.monitoring.active():
        print(f"\n== {alert['id']} {alert['device_id']}: {alert['signal']}")
        print(agent.ask(
            f"Monitoring alert {alert['id']} on {alert['device_id']}: {alert['signal']}. "
            "Diagnose it, apply a safe runbook fix if one fits, verify health afterwards, "
            "and open a ticket for the device owner documenting what you did.",
            on_tool=_show_tool))
    return 0


def cmd_verify(a: argparse.Namespace) -> int:
    ok, msg = AuditLog(AUDIT_PATH).verify()
    print(msg)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="opspilot")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("chat")
    c.add_argument("--user", required=True)
    c.add_argument("--role", default="employee", choices=["employee", "it_agent", "it_admin"])
    c.add_argument("--incident", action="store_true", help="declared P1: lifts change freezes")
    c.add_argument("--model", default=None)
    c.set_defaults(fn=cmd_chat)
    h = sub.add_parser("heal")
    h.add_argument("--model", default=None)
    h.set_defaults(fn=cmd_heal)
    sub.add_parser("audit-verify").set_defaults(fn=cmd_verify)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
