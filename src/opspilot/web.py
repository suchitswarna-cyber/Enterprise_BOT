"""Minimal web chat front end (standard library only).

  opspilot web --port 8080

All browser sessions share one set of (mock) enterprise systems, so tickets,
KB drafts and outage detection behave like one service desk. Tool execution is
serialised by a shared lock; model calls run concurrently.

Approvals: the page has an "approvers on call" field; when an action needs
approval, those ids are submitted as the approvers (the policy engine still
rejects self-approval and too few approvers). Production replaces this with
approval cards sent to the approver group in Teams or Slack.

Endpoints
  POST /api/session                 start a session {user, role, incident}
  POST /api/chat                    one turn, JSON reply when done
  POST /api/chat/stream             one turn, NDJSON progress events then a final "done"
  POST /api/undo                    roll back the session's last change
  GET  /api/tickets?session_id=     tickets visible to the session's user
  GET  /api/kb?session_id=          KB articles (drafts only for IT staff)
  POST /api/kb/review               publish/reject a draft (IT staff; policy + audit)
  GET  /api/stats                   dashboard metrics from the audit log
  GET  /api/audit                   last audit entries + hash-chain check
  POST /api/demo/reset              reset the mock systems (it_admin)
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

from . import analytics
from .audit import AuditLog
from .connectors import Systems
from .orchestrator import Orchestrator
from .policy import Requester, Risk

AUDIT_PATH = os.environ.get("OPSPILOT_AUDIT_LOG", "var/audit.jsonl")
PAGE = (Path(__file__).parent / "static" / "index.html").read_text()
ROLES = {"employee", "it_agent", "it_admin"}
STAFF = {"it_agent", "it_admin"}

SYSTEMS = Systems()
SYSTEMS_LOCK = threading.RLock()

Emit = Callable[[dict], None]


class Session:
    def __init__(self, user: str, role: str, incident: bool):
        from .agent import GeminiAgent

        self.user, self.role = user, role
        self.lock = threading.Lock()
        self.pending_approvers: list[str] = []
        self.approvals: list[dict] = []
        self.emit: Emit | None = None
        self.orch = Orchestrator(Requester(user, role, incident), AuditLog(AUDIT_PATH),
                                 systems=SYSTEMS, lock=SYSTEMS_LOCK, approver=self._approve)
        self.agent = GeminiAgent(self.orch)

    def _approve(self, plan: str, risk: Risk, required: int) -> list[str]:
        chosen = self.pending_approvers[:required]
        a = {"plan": plan, "risk": risk.name, "required": required, "approvers": chosen}
        self.approvals.append(a)
        if self.emit:
            self.emit({"type": "approval", **a})
        return chosen

    def run_turn(self, message: str, approvers: str, emit: Emit | None = None) -> dict:
        """Answer one message; optionally stream progress through `emit`. Records a
        `chat_turn` audit event (used by the dashboard)."""
        with self.lock:
            self.pending_approvers = [a.strip() for a in approvers.split(",") if a.strip()]
            self.approvals = []
            self.emit = emit
            tools: list[dict] = []

            def on_tool(name, args, result):
                tools.append({"tool": name, "args": args, "result": result})
                if emit:
                    emit({"type": "tool", "tool": name, "args": args, "result": result})

            started = time.monotonic()
            try:
                reply = self.agent.ask(message, on_tool=on_tool, on_status=emit)
            finally:
                self.emit = None
            duration = round(time.monotonic() - started, 2)

            fixes = sum(1 for t in tools if t["result"].get("ok") and t["tool"] in self.orch.tools
                        and self.orch.tools[t["tool"]].risk_for(t["args"]) > Risk.READ)
            ticket_created = any(t["tool"] == "create_ticket" and t["result"].get("ok") for t in tools)
            error = reply.startswith("[the AI service is unavailable")
            self.orch.audit.record("chat_turn", session=self.orch.session_id, requester=self.user, role=self.role,
                                   duration_s=duration, tools=len(tools), fixes=fixes,
                                   ticket_created=ticket_created, error=error, model=self.agent.model)
            return {"reply": reply, "tools": tools, "approvals": self.approvals, "model": self.agent.model,
                    "duration_s": duration, "undo_available": self.orch.journal.peek() is not None}


SESSIONS: dict[str, Session] = {}


def _ticket_view(t: dict) -> dict:
    keep = ("ticket_id", "title", "status", "priority", "requester", "kind", "created_at",
            "problem", "linked", "resolution", "notes")
    return {k: t[k] for k in keep if k in t}


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

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if url.path == "/api/audit":
            log = AuditLog(AUDIT_PATH)
            ok, msg = log.verify()
            lines = log.path.read_text().splitlines()[-25:] if log.path.exists() else []
            return self._send(200, {"ok": ok, "message": msg, "entries": [json.loads(x) for x in lines]})
        if url.path == "/api/stats":
            with SYSTEMS_LOCK:
                return self._send(200, analytics.compute(analytics.read_entries(AUDIT_PATH), SYSTEMS))

        s = SESSIONS.get(q.get("session_id", ""))
        if url.path in ("/api/tickets", "/api/kb") and s is None:
            return self._send(401, {"error": "start a session first"})
        if url.path == "/api/tickets":
            with SYSTEMS_LOCK:
                rows = SYSTEMS.tickets.list(None if s.role in STAFF else s.user)
                return self._send(200, {"scope": "all" if s.role in STAFF else "mine",
                                        "tickets": [_ticket_view(t) for t in rows]})
        if url.path == "/api/kb":
            with SYSTEMS_LOCK:
                staff = s.role in STAFF
                arts = [a for a in SYSTEMS.kb.list() if staff or a["status"] == "published"]
                return self._send(200, {"can_review": staff, "articles": arts})
        self._send(404, {"error": "not found"})

    # --------------------------------------------------------------- POST
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
            try:
                SESSIONS[sid] = Session(user, role, bool(b.get("incident")))
            except ValueError as e:  # e.g. GEMINI_API_KEY not set
                return self._send(503, {"error": f"Could not start the AI agent: {e} "
                                                 "Set GEMINI_API_KEY and restart `opspilot web`."})
            return self._send(200, {"session_id": sid, "model": SESSIONS[sid].agent.model})

        s = SESSIONS.get(b.get("session_id", ""))
        if s is None:
            return self._send(404, {"error": "unknown session; start a new one"})

        if self.path in ("/api/chat", "/api/chat/stream"):
            msg = str(b.get("message", "")).strip()
            if not msg:
                return self._send(400, {"error": "empty message"})
            approvers = str(b.get("approvers", ""))
            if self.path == "/api/chat":
                return self._send(200, s.run_turn(msg, approvers))
            return self._stream_turn(s, msg, approvers)

        if self.path == "/api/undo":
            with s.lock:
                r = s.orch.undo_last()
                return self._send(200, {"result": r, "undo_available": s.orch.journal.peek() is not None})

        if self.path == "/api/kb/review":
            r = s.orch.execute("review_kb_article", {"kb_id": str(b.get("kb_id", "")),
                                                     "decision": str(b.get("decision", "")),
                                                     "note": str(b.get("note", ""))})
            return self._send(200 if r.get("ok") else 403 if r.get("error") == "denied_by_policy" else 400, r)

        if self.path == "/api/demo/reset":
            if s.role != "it_admin":
                return self._send(403, {"error": "only it_admin can reset the demo data"})
            with SYSTEMS_LOCK:
                SYSTEMS.__init__()  # back to the seeded mock data, same object for every session
            s.orch.audit.record("demo_reset", session=s.orch.session_id, requester=s.user, role=s.role)
            return self._send(200, {"ok": True})

        self._send(404, {"error": "not found"})

    def _stream_turn(self, s: Session, msg: str, approvers: str) -> None:
        """NDJSON over a close-delimited HTTP/1.0 response: one JSON object per line."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        alive = True

        def emit(event: dict) -> None:
            nonlocal alive
            if not alive:
                return
            try:
                self.wfile.write((json.dumps(event, default=str) + "\n").encode())
                self.wfile.flush()
            except OSError:  # browser went away; finish the turn anyway so state stays consistent
                alive = False

        emit({"type": "start"})
        result = s.run_turn(msg, approvers, emit)
        emit({"type": "done", **result})


def serve(host: str = "127.0.0.1", port: int = 8080) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"OpsPilot web UI on http://{host}:{port}")
    httpd.serve_forever()
