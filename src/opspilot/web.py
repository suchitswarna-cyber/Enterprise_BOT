"""Minimal web chat front end (standard library only).

  opspilot web --port 8080

Approvals: the page has an "approvers on call" field; when an action needs
approval, those ids are submitted as the approvers (the policy engine still
rejects self-approval and too few approvers). Production replaces this with
approval cards sent to the approver group in Teams or Slack.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .audit import AuditLog
from .orchestrator import Orchestrator
from .policy import Requester, Risk

AUDIT_PATH = os.environ.get("OPSPILOT_AUDIT_LOG", "var/audit.jsonl")
PAGE = (Path(__file__).parent / "static" / "index.html").read_text()
ROLES = {"employee", "it_agent", "it_admin"}


class Session:
    def __init__(self, user: str, role: str, incident: bool):
        from .agent import GeminiAgent

        self.lock = threading.Lock()
        self.pending_approvers: list[str] = []
        self.approvals: list[dict] = []
        self.orch = Orchestrator(Requester(user, role, incident), AuditLog(AUDIT_PATH), approver=self._approve)
        self.agent = GeminiAgent(self.orch)

    def _approve(self, plan: str, risk: Risk, required: int) -> list[str]:
        chosen = self.pending_approvers[:required]
        self.approvals.append({"plan": plan, "risk": risk.name, "required": required, "approvers": chosen})
        return chosen


SESSIONS: dict[str, Session] = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the console quiet
        pass

    def _send(self, code: int, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if self.path == "/api/audit":
            log = AuditLog(AUDIT_PATH)
            ok, msg = log.verify()
            lines = log.path.read_text().splitlines()[-25:] if log.path.exists() else []
            return self._send(200, {"ok": ok, "message": msg, "entries": [json.loads(x) for x in lines]})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            b = self._body()
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})

        if self.path == "/api/session":
            user = str(b.get("user", "")).strip()
            role = b.get("role", "employee")
            if not user or role not in ROLES:
                return self._send(400, {"error": "user and a valid role are required"})
            sid = uuid.uuid4().hex
            SESSIONS[sid] = Session(user, role, bool(b.get("incident")))
            return self._send(200, {"session_id": sid, "model": SESSIONS[sid].agent.model})

        s = SESSIONS.get(b.get("session_id", ""))
        if s is None:
            return self._send(404, {"error": "unknown session; start a new one"})

        if self.path == "/api/chat":
            msg = str(b.get("message", "")).strip()
            if not msg:
                return self._send(400, {"error": "empty message"})
            with s.lock:
                s.pending_approvers = [a.strip() for a in str(b.get("approvers", "")).split(",") if a.strip()]
                s.approvals = []
                tools: list[dict] = []
                reply = s.agent.ask(msg, on_tool=lambda n, a, r: tools.append({"tool": n, "args": a, "result": r}))
                return self._send(200, {"reply": reply, "tools": tools, "approvals": s.approvals,
                                        "model": s.agent.model, "undo_available": s.orch.journal.peek() is not None})

        if self.path == "/api/undo":
            with s.lock:
                r = s.orch.undo_last()
                return self._send(200, {"result": r, "undo_available": s.orch.journal.peek() is not None})

        self._send(404, {"error": "not found"})


def serve(host: str = "127.0.0.1", port: int = 8080) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"OpsPilot web UI on http://{host}:{port}")
    httpd.serve_forever()
