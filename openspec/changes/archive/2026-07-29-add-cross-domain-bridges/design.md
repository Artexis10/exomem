# Design: add-cross-domain-bridges

## Context

Bridges are opt-in governance for a derived compiled note, not a new authority
system. With no governance policy, every surface takes the existing
byte-compatible fast path: it does not parse bridge bytes or create governance
state. With active governance, only bridge-shaped notes are subject to the
additional release gate; unrelated open notes retain their ordinary decision.

## Goals / Non-Goals

Goals: exact, audience-bound approval of a reviewed bridge; uniform admission
and provenance stripping; deterministic dependency staleness and read-only
review. Non-goals: automated abstraction or approval, a scope-lattice redesign,
and exposing confidential provenance through any result surface.

## Decisions

### D1 — Release is a strict exact-item gate

`kind: release` is a discriminated grant type, parsed separately from standing
grants, session grants, and the min/max scope lattice. A valid release binds the
canonical bridge path and stable ref, exact bridge SHA-256, `to_audience`, grant
id/reason/time, `bridge_scope`, and ordered source dependencies. Every dependency
binds stable ref, canonical path, exact hash, and an audience-scoped restriction
signature; grant options name exact provenance strip targets. Missing, unknown,
duplicate, ambiguous, copied, renamed, aliased, traversal/symlink, ref/path,
or audience-drifted identities fail closed. `bridge_scope`, bridge purpose, and
the bridge's ordinary ceiling can only narrow; a release never raises access.

### D2 — One exact bytes snapshot supplies all decisions

The same bytes are read, hashed, parsed, decided, receipted, and projected after
cache lookup. A centralized admission result is consumed by `_decide_path`,
direct and immutable reads, explain/simulate, page/unit/mixed search, graph,
pack, review context, and terminal filtering. Complete but unapproved bridge
metadata returns `RELEASE_UNAPPROVED`; an invalid approval, bridge-byte mismatch,
or any dependency drift returns path-free `RELEASE_STALE` on every surface.
Bridge scope membership is evaluated on the bridge alone, never unioned with a
source. Principal, approval, and dependency state remain post-cache with respect
to shared retrieval-cache entries; identity-keyed process decision memoization may
carry those facts.

### D3 — Restriction signatures are local and deterministic

Each dependency signature covers the approved audience, live sorted memberships,
matching scope constraints, intersecting rule id/kind/ceiling/purpose/
purpose-condition/typed canonically encoded options, and persistent standing
grants. It excludes global policy fingerprints and ephemeral session state.
Relevant membership, constraint, rule, or standing-grant changes stale a release;
unrelated scopes or audiences do not. Unsupported canonical option values fail
closed rather than crashing admission.

### D4 — Authoring and approval are separate reviewed actions

Normal `remember` and replacement use validate-only -> reviewed draft -> commit.
They accept bridge fields only all-or-none, normalize sources to stable refs, and
bind those fields into the draft hash/token; the committed note remains
unreleased. Owner-reviewed `govern_memory propose -> commit` is a distinct
approval or reapproval of the exact committed bridge and records receipt and
causation evidence. The host's explicit confirmation is the trust boundary; core
does not claim to distinguish an owner from an agent absent a host signal.

### D5 — Strip against approval-resolved targets, recursively

Projection copies are recursively stripped using approval-resolved dependencies,
even if a source never entered the result pool. Strip `bridge_of`, confidential
`bridge_scope`, sources/evidence/history/body Relations, inbound/outbound/raw
content, graph seed/node/edge, relation/supersession/parent/matched-unit fields,
and title/path/ref plus encoded, case, bare, and link aliases. Direct bridge raw
content is omitted unless safely reconstructed. Canonical data and shared caches
are never mutated.

### D6 — L2 constraints and review lifecycle remain narrow

One scope-registered, validated constraint string is emitted only when the
ordinary decision permits L2. Multiple distinct applicable strings refuse the
constraint below L2; strings are provenance-free. This set-based path is separate
from legacy rule-option fallback. `bridge_review` is a read-only default attention
category with only bridge path and generic causes: due, bridge edited, source or
restriction changed, and source unavailable/ambiguous. Signal versions cover
bridge bytes, review date, approval identity, cause, and dependency digest, not
today. Due is not expiry; triage never approves; audience partitions are
independent; exact reapproval clears stale findings.

## Risks / Trade-offs

Approval remains a human review decision: exact content binding prevents a prior
approval from releasing later edits, but cannot judge whether the approved
abstraction itself is appropriate. The host confirmation boundary is therefore
documented rather than overstated.

## Migration Plan

Additive metadata and release grants. Existing notes remain ordinary unless they
carry bridge metadata under active governance; only exact, separately approved
bridges release.

## Open Questions

None.
