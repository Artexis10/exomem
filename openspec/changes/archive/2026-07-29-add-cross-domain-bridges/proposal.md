# Proposal: add-cross-domain-bridges

## Why

A confidential source may inform an open-domain claim only through a reviewed,
exactly approved derived note. This change makes that crossing explicit without
weakening ordinary notes: policy-free vaults retain their byte-compatible
no-parse/no-state fast path, and active but unrelated governance leaves ordinary
open notes frictionless.

## What Changes

- A bridge is an ordinary compiled note with all-or-none `bridge_of`,
  `bridge_scope`, and `bridge_review` metadata. With active governance, complete
  bridge metadata has no release without one exact `kind: release` approval and
  is withheld as `RELEASE_UNAPPROVED`.
- A release grant is an exact-item gate, separate from standing and session
  grants and the scope lattice. It binds canonical bridge path, stable ref,
  exact bridge bytes hash, audience, grant/reason/time, scope, each resolved
  source ref/path/hash, audience-scoped restriction signature, and strip
  targets. It may only narrow the bridge's ordinary decision; it never widens it.
- The normal `remember`/replace validate-review-commit route can create an
  unreleased bridge draft with normalized stable refs and metadata bound into its
  draft. A separate owner-reviewed `govern_memory propose -> commit` releases or
  re-releases that exact draft and emits the existing receipt/causation evidence.
  Host confirmation is the owner trust boundary.
- One post-cache admission result governs direct and immutable reads,
  explain/simulate, all search modes, graph, packs, review context, and terminal
  filtering. Hash or dependency drift is path-free `RELEASE_STALE` everywhere.
- Released bridges recursively strip approval-resolved restricted provenance from
  every response projection without mutating canonical notes or shared caches.
- Scope constraint strings are deterministic L2-only micro-bridges. Bridge
  review becomes a read-only, per-audience default attention category; review
  dates prompt review but do not expire an otherwise exact release.

## Capabilities

### New Capabilities

- `cross-domain-bridges`: exact, audience-bound release approvals for derived
  notes; deterministic L2 constraints; release-staleness and review lifecycle.

### Modified Capabilities

- `attention-queue`: read-only `bridge_review` findings for due, edited, changed,
  and unavailable bridge dependencies.

## Impact

- Governance parsing/admission/projection, normal authoring and replacement,
  owner-reviewed policy commits, audit/attention, and public tool contracts.
- Focused bridge, egress, authoring, attention, review-state, receipt/recovery,
  bootstrap, schema/connector/plugin/capability, latency, lint, build, and
  strict OpenSpec verification.
- Out of scope: automatic abstraction quality, automatic approval, and any
  widening of ordinary governance rules.
