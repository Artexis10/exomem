# Proposal

## Why

An installed audit on 2026-11-05 continued after its request budget and client timeout, so the caller received no report.
The audit needs a durable contract for incomplete coverage and safe delivery when its existing deadline expires.

## What Changes

- Reuse the existing request budget throughout corpus preparation and audit checks.
- Return findings only from completed checks, with explicit incomplete coverage for work that stopped or never started.
- Keep the final authorization check fresh. Return a small budget error if authorization cannot finish before delivery.
- Preserve complete results when the work fits, and preserve the existing exemptions for local and reconcile calls.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `connector-request-budget`: make audit preparation, checks and result delivery consume the existing request deadline.
- `action-first-audit`: distinguish completed checks from incomplete coverage without asserting that missing findings prove absence.

## Impact

The change affects `audit.py`, its expensive read dependencies, the audit facade, and the existing final result filter.
It reuses `RequestBudget` and the existing structured error carrier. It adds no dependency, model, background job or timeout override.
Heavy checks remain explicitly selected through the existing surface; this proposal adds no optional capability to enable.
Deadline exhaustion produces partial evidence or a clear budget error, never a silent empty success.

Implementation, merge, release and a further personal service promotion require approval of this contract and its plan.
Private request identifiers, findings and diagnostic artifacts remain outside the repository.
