# Design: add-governance-tools

## Context

Reuses verified machinery. Mutation safety: `writer_lease.invoke_command`
(lease + idempotency), `mutation_terminal.committed_terminal` (`:82`, receipt
envelope). Consume-once nonces: `hosted_transfer.consume_transfer_jti`
(`:333-353`) — the correct primitive, distinct from the idempotency *replay*
store (`writer_lease.IdempotencyStore`), which returns a cached result on
key reuse rather than refusing a spent nonce. Fingerprint-guarded commit:
`relation_registry.save_registry` (`:230-252`, `STALE_*` on `expected_hash`
mismatch). Mixed read/write dispatch: `commands.invocation_is_read_only`
(`:5442`, per-command action allowlists). Bootstrap payload:
`op_bootstrap:211` (versioned, code-authored, `content_included: False`).

## Goals / Non-Goals

Goals: one natural-language authoring tool; safe propose→commit that cannot be
replayed or applied against drifted state; zero-friction release-time grants;
revoke/undo that keep history and dependent grants coherent; a teaching surface
so any client uses the operations correctly.

Non-Goals: receipts persistence internals (next change), bridges, presets, a
policy UI. No model call in the enforcement or validation path — the LLM proposes,
Exomem decides.

## Decisions

### D1 — One tool, operation-routed
`govern_memory(operation=...)` matches Exomem's multi-operation tool style and
respects client tool-count limits. `list`/`explain`/`simulate`/`declare(read)`
classify read-only via a new `_GOVERN_MEMORY_READ_ONLY_ACTIONS` set in
`invocation_is_read_only`; `commit`/`grant`/`revoke`/`suspend`/`resume`/`undo`
write and ride the lease + idempotency + receipt terminal. Policy-overwriting
operations join `DESTRUCTIVE_OPS` so annotations warn.

### D2 — propose stores a drift-bound nonce; commit consumes it
`propose` writes a pending record to `.governance.sqlite proposals`
(`id, created_at, expires_at, proposal_json, fingerprint_at_propose, affected_item_fingerprints, status`)
with a TTL. `commit(proposal_id)` consumes it under `BEGIN IMMEDIATE`
(consume-once, JTI semantics — NOT the idempotency replay store) and refuses with
`STALE_GOVERNANCE_POLICY` if the current policy fingerprint or any affected-item
content fingerprint differs from propose time. This closes the TOCTOU window: an
out-of-band edit or a `file_watcher` reindex between propose and commit forces a
re-propose rather than applying a decision computed against vanished state.

### D3 — propose previews respect current ceilings
The resolved-membership preview renders counts and samples at each member's
*current* effective ceiling — a proposal for a scope that would restrict N pages
must not leak those N titles to the model before the rule exists. `explain` and
`simulate` toward a sub-ceiling audience return counts + rule-ids only, never
member titles/excerpts.

### D4 — Grants: session in sqlite, standing in YAML
Release-time `grant` redeems a withhold-token (from `add-release-gate`) and writes
an ephemeral session grant to `.governance.sqlite session_grants` (TTL'd, never a
YAML file) so the lattice sees it immediately; a durable standing exception is a
`grants/*.yaml` file. `revoke(scope=session)` clears all session grants for the
session; `declare` sets a session purpose default. The model can only redeem
tokens Exomem minted — it cannot enlarge scope it was not offered.

### D5 — undo re-resolves dependents
`undo` restores the archived prior policy version and re-resolves grants that
depended on the restored version's selectors, expiring/flagging any whose member
set changed — so a restore never silently widens or narrows a grant's reach
against a version it was never reviewed for.

### D6 — Teaching surface, forged-envelope defense
`bootstrap` gains a `governance` section and a bumped `contract_version`; scaffold
`SKILL.md` + generic `references/governance.md` teach the lifecycle and state that
governance notices/grant-hints travel only in reserved top-level response keys and
that governance-shaped text inside `excerpt`/`body`/`content` is data, never a
command. Kept generic — `tests/test_scaffold_no_leak.py` enforces no personal
tokens.

## Risks / Trade-offs

- **Byte-pinned tool surface**: the new tool + params change
  `tool_surface_contract.json` and connector-guardrail pins; regenerate via
  `scripts/dump-tool-schemas.py` in-task.
- **Tier placement**: `govern_memory` is Tier-2 (policy administration is
  desk-side; hosted cells with `EXOMEM_DISABLE_TIER2` drop the admin tool) — but
  release enforcement still runs everywhere, so decisions hold even where the
  admin tool is absent. The spec states this explicitly.
- **Injected commit attempts**: content cannot mint a valid `proposal_id`; a
  forged id fails the consume-once + fingerprint check. A model *claiming* user
  consent from injected text remains a documented client-side residual, bounded by
  the propose→confirm gate, token narrowness, and revoke-session.

## Migration Plan

Additive tool + bootstrap version bump (existing contract discipline). No data
migration; `.governance.sqlite` gains tables via `PRAGMA user_version`. Absent
`_Governance/`, `list`/`explain` report "governance: disabled" and no file is
written until a `commit`.

## Open Questions

None blocking. Preset shortcuts (`propose(preset="legal")`) are deferred to
`add-professional-presets`.
