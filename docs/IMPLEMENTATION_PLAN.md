# Implementation plan

The repository already contains phase 0: a working core with mock connectors.
Each later phase is independently shippable.

## Phase 0: Core prototype (done)

- [x] Agent loop on Gemini function calling, manual execution
- [x] Policy engine: risk tiers, role ceilings, self-service, freeze windows, 1- and 2-person approval
- [x] Context-aware risk (privileged groups, dangerous runbooks)
- [x] Dry-run plans shown to approvers
- [x] Rollback journal and `/undo`
- [x] Hash-chained audit log and `audit-verify`
- [x] Blast-radius limiter
- [x] Duplicate-ticket detection, KB-first troubleshooting, KB drafts from resolutions
- [x] Self-healing pass over monitoring alerts (`opspilot heal`)
- [x] Offline test suite, including the agent loop with a scripted fake model

## Phase 1: Pilot with one real system (2–3 weeks)

1. Choose the first integration by ticket volume. Password and unlock requests are usually #1.
2. Implement a `GraphDirectory` connector (Entra ID) with the same methods as `MockDirectory`.
   Use an app registration with least-privilege Graph permissions and a client secret in Key Vault.
3. Implement an ITSM connector (ServiceNow Table API or Jira Service Management).
4. Persist audit and journal to Postgres; keep the hash chain.
5. Run in **shadow mode**: the agent proposes actions, humans execute them, and you compare outcomes.
6. Build an eval set from 100–200 historical tickets and score resolution accuracy and policy adherence.

## Phase 2: Chat channel and approvals (2 weeks)

1. Teams bot (Bot Framework) or Slack app (Bolt) as a front end over the same orchestrator.
2. Approvals as adaptive cards sent to the approver group, with a timeout and escalation.
3. Map SSO identity to `Requester` (role from IdP group claims).
4. Enable self-service for unlock, password reset and ticket status for all employees.

## Phase 3: Endpoint automation and self-healing (3 weeks)

1. Intune or Jamf connector: device health, plus runbooks as remediation scripts.
2. Monitoring webhooks into a queue, then `heal` workers, with per-device rate limits.
3. Expand the runbook catalog. Every runbook gets an owner, a test, and an inverse where one exists.
4. Freeze calendar fed from the change-management system.

## Phase 4: Knowledge flywheel and analytics (2 weeks)

1. Embeddings-based KB search and incident clustering (spike detection: N similar tickets in M minutes means a P2 problem ticket).
2. KB drafts go to a review queue; publish on approval.
3. Dashboard: deflection rate, MTTR, approvals per day, denials by reason, rollbacks.

## Phase 5: Hardening

- Red-team prompt injection through ticket and KB content; add output filters for PII.
- Load test; set per-tenant model quotas; retries with backoff on the model API.
- DR: replay the audit log to rebuild state; periodic `audit-verify` in CI or cron.
- Access reviews: monthly report of all access grants made by the agent.

## Technology choices (to decide)

| Layer | Prototype | Production options |
|---|---|---|
| Model | Gemini (`gemini-3.5-flash`, configurable) | Gemini Pro for hard diagnostics; Flash for chat |
| Runtime | Python CLI | FastAPI on Cloud Run / AKS / ECS |
| State | in-memory + JSONL | Postgres + Redis |
| Channels | CLI | Teams, Slack, web portal |
| Identity | mock | Entra ID, Okta, on-prem AD via LDAP |
| ITSM | mock | ServiceNow, Jira Service Management, Freshservice |
| Endpoints | mock | Intune, Jamf, SCCM |
| Secrets | env var | Azure Key Vault, AWS Secrets Manager, GCP Secret Manager |
