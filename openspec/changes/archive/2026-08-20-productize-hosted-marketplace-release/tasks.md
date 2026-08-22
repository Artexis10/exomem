## 1. Freeze Current Platform Contracts

- [x] 1.1 Add failing tests that preserve the registered `plugin_asdk_app_*` package technical identity, reject legacy bare IDs in package fields, keep Claude's `mcpServers` wrapper, and emit the current OpenAI `.mcp.json` connection shape
- [x] 1.2 Update the existing OpenAI candidate renderer and validators without changing package identity or creating a parallel packaging path
- [x] 1.3 Rerender and verify deterministic candidates and release locks for every affected platform artifact

## 2. Canonical Marketplace Inputs

- [x] 2.1 Add failing tests for canonical common/channel listing data, provider field limits, public URL requirements, and identity drift
- [x] 2.2 Add a generic canonical marketplace definition plus governed positive, negative, and do-not-capture review cases
- [x] 2.3 Define schemas that prohibit private-value fields and extend high-confidence credential/path leak guards across marketplace definitions, cases, packets, evidence, and receipts with value-free diagnostics

## 3. Deterministic Provider Packets

- [x] 3.1 Add failing tests for deterministic Claude Connector, Claude Plugin, and universal OpenAI submission packets
- [x] 3.2 Render provider-shaped redacted packets with complete tool contracts, prompts, review cases, artifact bindings, explicit non-UI screenshot handling, and no requirement for a not-yet-issued directory identity
- [x] 3.3 Add `directory-check` and `directory-render` CLI commands and validate checked-in generated packets against the canonical inputs
- [x] 3.4 Carry deterministic official-form metadata (verified brand asset, category, docs/setup URLs, use cases, read/write capability) and enforce current Claude listing limits

## 4. Readiness and Publication State

- [x] 4.1 Add failing tests for signed-evidence schema/TTL/binding failures, pending/stale promotion refusal, public-admission blockers, append-only revisions, approved state, active-pointer CAS conflicts, digest drift, rejection, and exact withdrawal
- [x] 4.2 Implement fail-closed per-channel readiness derived from exact live promotion bindings plus versioned operator-signed production, prerequisite, public-admission, and post-install evidence
- [x] 4.3 Implement append-only draft/submitted/in-review/approved/published/rejected/withdrawn revisions and an atomic compare-and-swap active-publication pointer per channel
- [x] 4.4 Add `directory-status` and `directory-record` CLI commands without changing the existing runtime promotion state machine or tenant data
- [x] 4.5 Bind every evidence and receipt path to an explicit trusted deployment SHA; require exact evidence schemas, current normalized tool-contract probes, Origin rejection, and response minimization
- [x] 4.6 Separate provider publication from post-install CAS activation; retain per-listing-version heads and make append, activation, and withdrawal recovery idempotent
- [x] 4.7 Require exact receipt bindings, public HTTPS URLs, OpenAI registered/package versus directory identities, and reject marketplace leaks and OpenAI subscription/checkout/upsell copy
- [x] 4.8 Normalize explicit null CAS CLI inputs, enforce raw-UTF-8 OpenAI directory-ID hash binding, exact submission schemas, value-type-independent private-field guards, whole-packet sale scanning, and sale-free output evidence

## 5. Product Journey and Operator Runbook

- [x] 5.1 Replace manual-first hosted client guidance with directory install/connect, one OAuth sign-in, and natural recall/capture; retain global custom instructions only as a fallback
- [x] 5.2 Document the three distinct provider channels and surface matrix, reviewer-account seeding, external secret ownership, signed evidence, revisioned submission, rejection, publication, smoke testing, incident withdrawal, and recovery
- [x] 5.3 Document and validate paired Substrate product/policy/domain/OAuth/MCP requirements plus the post-friends public-admission and hosted cost-control gate; explicitly forbid subscription sales through OpenAI plugin interactions
- [x] 5.4 Document deployment-bound `directory-record` and explicit `directory-activate` CAS commands, post-install evidence location, retry recovery, and per-version status interpretation

## 6. Static Verification and Delivery

- [x] 6.1 Run strict OpenSpec validation, focused marketplace/package tests, deterministic rerender checks, leak tests, and Ruff on every changed Python surface
- [x] 6.2 Obtain an independent architecture/security review and address actionable findings
- [x] 6.3 Obtain an independent verifier pass over the CLI, generated packets, and regression suite
- [x] 6.4 Commit the intended Exomem scope, integrate current remote main, push the feature branch, and open a ready pull request with verification evidence

## 7. External Launch Handoff

The code change ends when tasks 1–6 are complete. Production deployment, public-admission approval, exact client promotion, provider submission/review, and post-directory-install acceptance remain operator-controlled launch work documented by the generated runbook and the existing `add-hosted-client-plugins` live gates; they are never checked off from repository-only evidence.
