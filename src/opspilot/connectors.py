"""Connectors to enterprise systems.

Each connector is a small interface. The in-memory `Mock*` classes let the bot
run end to end with no infrastructure; production adapters (Entra ID / Okta,
ServiceNow / Jira SM, Intune / Jamf, Datadog / Prometheus, Confluence) implement
the same methods. Credentials live only inside connectors and never reach the model.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field


# --------------------------------------------------------------------- identity
@dataclass
class MockDirectory:
    users: dict[str, dict] = field(default_factory=lambda: {
        "alice": {"name": "Alice Chen", "dept": "Finance", "manager": "dana",
                  "locked": True, "enabled": True, "mfa_enrolled": True,
                  "groups": ["finance-users", "vpn-users"]},
        "bob": {"name": "Bob Okafor", "dept": "Engineering", "manager": "dana",
                "locked": False, "enabled": True, "mfa_enrolled": False,
                "groups": ["eng-users"]},
        "dana": {"name": "Dana Ruiz", "dept": "IT", "manager": None,
                 "locked": False, "enabled": True, "mfa_enrolled": True,
                 "groups": ["it-admins"]},
    })
    groups: set[str] = field(default_factory=lambda: {
        "finance-users", "vpn-users", "eng-users", "it-admins", "prod-db-readers"})
    notifications: list[str] = field(default_factory=list)

    def _user(self, user_id: str) -> dict:
        if user_id not in self.users:
            raise KeyError(f"no such user '{user_id}'")
        return self.users[user_id]

    def get_user(self, user_id: str) -> dict:
        return {"user_id": user_id, **self._user(user_id)}

    def set_locked(self, user_id: str, locked: bool) -> None:
        self._user(user_id)["locked"] = locked

    def set_enabled(self, user_id: str, enabled: bool) -> None:
        self._user(user_id)["enabled"] = enabled

    def reset_password(self, user_id: str) -> str:
        # The temporary credential goes out of band (SMS / manager); only a receipt
        # comes back, so no secret ever enters the model's context.
        self._user(user_id)
        receipt = f"pwd-{uuid.uuid4().hex[:8]}"
        self.notifications.append(f"temp password for {user_id} sent to registered device ({receipt})")
        return receipt

    def add_to_group(self, user_id: str, group: str) -> bool:
        if group not in self.groups:
            raise KeyError(f"no such group '{group}'")
        g = self._user(user_id)["groups"]
        if group in g:
            return False
        g.append(group)
        return True

    def remove_from_group(self, user_id: str, group: str) -> bool:
        g = self._user(user_id)["groups"]
        if group not in g:
            return False
        g.remove(group)
        return True


# -------------------------------------------------------------------- ticketing
def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 2}


@dataclass
class MockTicketing:
    tickets: dict[str, dict] = field(default_factory=lambda: {
        "INC-1001": {"title": "VPN disconnects every 10 minutes", "status": "open",
                     "priority": "P3", "requester": "bob", "notes": []},
        "INC-1002": {"title": "VPN keeps dropping connection on home wifi", "status": "open",
                     "priority": "P3", "requester": "alice", "notes": []},
    })
    _seq: int = 1003

    def create(self, title: str, description: str, priority: str, requester: str) -> str:
        tid = f"INC-{self._seq}"
        self._seq += 1
        self.tickets[tid] = {"title": title, "description": description, "status": "open",
                             "priority": priority, "requester": requester, "notes": []}
        return tid

    def get(self, ticket_id: str) -> dict:
        if ticket_id not in self.tickets:
            raise KeyError(f"no such ticket '{ticket_id}'")
        return {"ticket_id": ticket_id, **self.tickets[ticket_id]}

    def add_note(self, ticket_id: str, note: str) -> None:
        self.get(ticket_id)
        self.tickets[ticket_id]["notes"].append(note)

    def set_status(self, ticket_id: str, status: str, resolution: str = "") -> None:
        self.get(ticket_id)
        self.tickets[ticket_id]["status"] = status
        if resolution:
            self.tickets[ticket_id]["resolution"] = resolution

    def find_similar(self, text: str, threshold: float = 0.25) -> list[dict]:
        """Jaccard similarity over title tokens; swap for embeddings in production."""
        q = _tokens(text)
        out = []
        for tid, t in self.tickets.items():
            if t["status"] == "closed":
                continue
            k = _tokens(t["title"])
            score = len(q & k) / len(q | k) if q | k else 0.0
            if score >= threshold:
                out.append({"ticket_id": tid, "title": t["title"], "score": round(score, 2)})
        return sorted(out, key=lambda r: -r["score"])


# -------------------------------------------------------------------- endpoints
# Remediations are an allow-listed catalog, never arbitrary shell. Each entry
# names its inverse (or None when it is naturally idempotent / self-reverting).
RUNBOOKS: dict[str, dict] = {
    "clear_temp_files": {"desc": "Delete temp/cache files to free disk", "frees_gb": 6.0, "inverse": None},
    "restart_print_spooler": {"desc": "Restart the print spooler service", "inverse": None},
    "flush_dns": {"desc": "Flush DNS resolver cache", "inverse": None},
    "restart_vpn_client": {"desc": "Restart the VPN client service", "inverse": None},
    "enable_firewall": {"desc": "Turn the host firewall on", "inverse": "disable_firewall"},
    "disable_firewall": {"desc": "Turn the host firewall off", "inverse": "enable_firewall"},
}


@dataclass
class MockEndpoints:
    devices: dict[str, dict] = field(default_factory=lambda: {
        "LT-ALICE-01": {"owner": "alice", "os": "Windows 11", "disk_free_gb": 2.1,
                        "cpu_pct": 35, "days_since_patch": 41, "firewall": True,
                        "services": {"print_spooler": "stopped", "vpn_client": "running"}},
        "LT-BOB-07": {"owner": "bob", "os": "macOS 15", "disk_free_gb": 88.0,
                      "cpu_pct": 12, "days_since_patch": 3, "firewall": False,
                      "services": {"vpn_client": "degraded"}},
    })

    def health(self, device_id: str) -> dict:
        if device_id not in self.devices:
            raise KeyError(f"no such device '{device_id}'")
        d = self.devices[device_id]
        findings = []
        if d["disk_free_gb"] < 5:
            findings.append("low disk space")
        if d["days_since_patch"] > 30:
            findings.append("patches overdue")
        if not d["firewall"]:
            findings.append("firewall disabled")
        findings += [f"{s} is {st}" for s, st in d["services"].items() if st != "running"]
        return {"device_id": device_id, **d, "findings": findings}

    def devices_for(self, owner: str) -> list[str]:
        return [k for k, v in self.devices.items() if v["owner"] == owner]

    def run(self, device_id: str, runbook: str) -> str:
        if runbook not in RUNBOOKS:
            raise KeyError(f"runbook '{runbook}' is not in the approved catalog")
        d = self.devices[device_id] if device_id in self.devices else None
        if d is None:
            raise KeyError(f"no such device '{device_id}'")
        if runbook == "clear_temp_files":
            d["disk_free_gb"] = round(d["disk_free_gb"] + RUNBOOKS[runbook]["frees_gb"], 1)
        elif runbook == "restart_print_spooler":
            d["services"]["print_spooler"] = "running"
        elif runbook == "restart_vpn_client":
            d["services"]["vpn_client"] = "running"
        elif runbook == "enable_firewall":
            d["firewall"] = True
        elif runbook == "disable_firewall":
            d["firewall"] = False
        return f"{runbook} completed on {device_id}"


# ------------------------------------------------------------------- monitoring
@dataclass
class MockMonitoring:
    alerts: list[dict] = field(default_factory=lambda: [
        {"id": "ALR-77", "device_id": "LT-ALICE-01", "signal": "disk_free_gb < 5", "severity": "warning"},
        {"id": "ALR-78", "device_id": "LT-BOB-07", "signal": "vpn_client degraded", "severity": "warning"},
    ])

    def active(self) -> list[dict]:
        return list(self.alerts)


# --------------------------------------------------------------- knowledge base
@dataclass
class MockKnowledgeBase:
    articles: dict[str, dict] = field(default_factory=lambda: {
        "KB-12": {"title": "Fix: VPN drops on home Wi-Fi",
                  "body": "Restart the VPN client service; if it persists, set Wi-Fi power saving off.",
                  "status": "published"},
        "KB-19": {"title": "Printer jobs stuck in queue",
                  "body": "Restart the print spooler service, then resend the job.",
                  "status": "published"},
    })
    _seq: int = 100

    def search(self, query: str) -> list[dict]:
        q = _tokens(query)
        hits = []
        for kid, a in self.articles.items():
            score = len(q & _tokens(a["title"] + " " + a["body"]))
            if score:
                hits.append((score, {"kb_id": kid, "title": a["title"], "body": a["body"]}))
        return [h for _, h in sorted(hits, key=lambda x: -x[0])][:3]

    def draft(self, title: str, body: str, source_ticket: str) -> str:
        kid = f"KB-{self._seq}"
        self._seq += 1
        self.articles[kid] = {"title": title, "body": body, "status": "draft", "source": source_ticket}
        return kid


@dataclass
class Systems:
    directory: MockDirectory = field(default_factory=MockDirectory)
    tickets: MockTicketing = field(default_factory=MockTicketing)
    endpoints: MockEndpoints = field(default_factory=MockEndpoints)
    monitoring: MockMonitoring = field(default_factory=MockMonitoring)
    kb: MockKnowledgeBase = field(default_factory=MockKnowledgeBase)
