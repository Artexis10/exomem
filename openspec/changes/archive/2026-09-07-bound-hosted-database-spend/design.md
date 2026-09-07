## Context

See proposal.md. The shared Neon database serves Substrate and hosted lifecycle/OAuth state. Existing one-second worker polling, SQL-backed readiness and minute maintenance keep it awake. The measured minimum 0.25 CU consumes 180 CU-hours over 30 days, independently of the small stored data set.

## Goals / Non-Goals

**Goals:** Verify cost controls without querying PostgreSQL, creating previews or changing provider state. Distinguish capacity, warning thresholds and actual consumption cutoffs. Make the preflight rerunnable by an agent.

**Non-Goals:** Automatic quota changes, hard-stop consent, email inbox access, invoice reconciliation, idle scheduling, database migration or a new monitoring daemon.

## Decisions

Use a small standard-library Python command, `infra/scripts/audit_hosted_database_budget.py`, over the authenticated Neon CLI's API v2 GET operations. The CLI owns OAuth refresh and credential storage; the audit never reads credential files or prints provider bodies/errors. An explicit `--neonctl` executable path supports pinned installations. No shell execution, arbitrary API URL, write verb or database connection is accepted. Bound each child invocation with a timeout.

Read project metadata, endpoint inventory, branch inventory and the owning organization's spending threshold. Derive the organization from the returned, identity-checked project; validate identifiers before interpolation. Each unavailable/malformed response becomes a named unknown finding and a nonzero exit. Independent inventory reads continue after another read fails where their target is still known.

Default policy is one endpoint and one production branch, minimum 0.25 CU, endpoint maximum at most 1 CU, autosuspend not disabled and an enabled spending alert no higher than 500 cents. A stricter positive setting passes. Report unexpected branches; never delete them. The optional `--monthly-compute-hours` checks an already authorized project quota, in weighted CU-seconds (hours multiplied by 3600). Omitting this option does not require or authorize a cutoff. A zero/missing provider quota is unlimited, not zero spend.

Emit only a bounded JSON report with status, finding codes, numerical observations, billing-period dates and control semantics. Distinguish confirmed failures from unknown state. The native alert's configuration can be verified, but email delivery cannot be inferred from that configuration. Current provider usage can lag; report its period without promising real-time accounting or a total-dollar ceiling.

The operational runbook makes this audit a preflight before changing database capacity or expanding hosted load. Provider-side alerts and any explicitly selected quota operate continuously; the audit itself is on demand, not background monitoring. It documents the currently applied 0.25–1 CU / $5 posture, safe read-modify-read operations and a separately approved hard-stop choice. No personal IDs or credential values are committed.

## Risks / Trade-offs

- Shared-database quota exhaustion stops OAuth, billing, lifecycle and gateway SQL → require explicit whole-service cutoff acceptance; do not enable a quota from this audit.
- Native spending alerts do not stop usage and compute quotas do not cap storage/transfer → report those semantics explicitly.
- A capacity cap retains the always-awake baseline → do not claim an idle-cost fix or Free-plan fit.
- Provider settings can drift after a successful read → timestamp reports and repeat at deployment; native controls remain the enforcement authority.
- Authenticated provider output can contain sensitive data → whitelist report fields, suppress raw subprocess output, and test sentinel redaction on failures.

## Migration Plan

Ship the read-only command and runbook without changing any running application or schedule. Run it against the existing project and preserve its sanitized result. Reverting this tooling does not alter live provider controls; reverting a control requires its own explicit operator change and verification.
