# OpsPilot: agentic IT support and automation bot

OpsPilot resolves IT requests end to end: unlocking accounts, resetting passwords,
diagnosing and fixing devices, granting access, and handling tickets. Every action
it takes is policy-checked, approved when needed, audited and reversible.

The model (Google Gemini) does the reasoning. A deterministic orchestrator holds the
authority. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## What makes it different

| Feature | What it does |
|---|---|
| **Risk-tiered policy engine** | Every tool is READ, LOW, HIGH or CRITICAL; roles have ceilings; employees act only on themselves |
| **Context-aware risk** | The same tool changes tier with its arguments: granting `vpn-users` is HIGH, `it-admins` is CRITICAL |
| **Dry-run plans for approvers** | Approvers see exactly what will happen before anything runs |
| **Two-person rule** | CRITICAL actions need two distinct approvers, and nobody approves their own request |
| **One-command rollback** | Every change records its inverse; `/undo` reverses it through the same policy path |
| **Tamper-evident audit** | SHA-256 hash-chained log; editing or deleting a line is detected |
| **Blast-radius limiter** | A session hands off to a human after N changes, which caps damage from a runaway loop |
| **Change-freeze awareness** | Risky changes are blocked during freeze windows unless P1 incident mode is declared |
| **Self-healing** | `opspilot heal` turns monitoring alerts into diagnosis, a safe runbook fix, verification and a ticket |
| **Duplicate and incident clustering** | New tickets are matched against open ones, so one outage doesn't become 50 tickets |
| **Knowledge flywheel** | Every resolution drafts a KB article for review, and the agent searches the KB first |
| **Secrets never in context** | Password resets go out of band; the model only sees a receipt |
| **Allow-listed remediation** | Fixes come from a runbook catalog, never arbitrary shell commands |

## Quick start

```bash
pip install -e ".[dev]"
```

```bash
export GEMINI_API_KEY=your-key   # from Google AI Studio; never commit it
```

```bash
opspilot chat --user alice --role employee
```

Try: *"I'm locked out and my laptop says the disk is full"*, *"Give bob access to it-admins"*
(as `--role it_agent`, which is denied), or `/undo` after a change.

Other commands:

```bash
opspilot chat --user dana --role it_admin
```

```bash
opspilot heal
```

```bash
opspilot audit-verify
```

Set `GEMINI_MODEL` to choose a model (default `gemini-flash-latest`).

## Tests

```bash
pytest
```

The suite runs offline. It covers the policy engine, approvals, rollback, audit tamper
detection, and the full agent loop against a scripted fake model.

## Layout

```
src/opspilot/
  agent.py         Gemini function-calling loop (auto-execution disabled)
  orchestrator.py  validate -> policy -> approval -> execute -> audit -> journal
  policy.py        risk tiers, roles, freezes, approver rules
  tools.py         tool registry: schema, risk, subject, preview, handler, inverse
  connectors.py    identity / ITSM / endpoint / monitoring / KB (mocks + interfaces)
  audit.py         hash-chained audit log
  rollback.py      undo journal
  cli.py           chat, heal, audit-verify
docs/
  ARCHITECTURE.md
  IMPLEMENTATION_PLAN.md
```

## Roadmap

Phase 0 (this repo) runs against mock systems. Next come real connectors (Entra ID,
ServiceNow), a Teams or Slack front end with approval cards, then endpoint
self-healing. See [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md).
