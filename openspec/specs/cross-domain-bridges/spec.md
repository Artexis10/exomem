# cross-domain-bridges Specification

## Purpose
Permit exact owner-approved cross-domain derived artifacts without widening
source policy, leaking restricted provenance, or sacrificing immutable snapshot
and review guarantees, while preserving frictionless ordinary notes.

## Requirements

### Requirement: Governance-free and ordinary notes remain frictionless

With an empty policy, every surface SHALL preserve its byte-compatible existing
behavior and SHALL NOT parse bridge metadata or create governance state. With
active governance, a note without bridge metadata SHALL retain its ordinary
decision even if unrelated policy exists.

#### Scenario: Empty policy does not invoke bridge machinery

- **WHEN** a bridge-shaped note is read or searched with no policy
- **THEN** the result follows the ordinary path without bridge parsing or
  governance-state creation

#### Scenario: Active unrelated policy does not gate an open note

- **WHEN** an active policy does not govern an ordinary open note
- **THEN** the note remains available under its ordinary decision

### Requirement: Release approval is an exact, non-widening bridge gate

With active governance, a bridge SHALL be an ordinary compiled note containing
exactly `bridge_of`, `bridge_scope`, and `bridge_review`, a canonical stable
bridge ref, and unique canonical stable source refs. Complete bridge metadata
without one matching release grant SHALL be withheld as `RELEASE_UNAPPROVED`.
A release grant SHALL be a strict `kind: release` type, separate from standing
and session grants and the scope lattice. It SHALL bind canonical bridge path and
stable ref, exact bridge bytes SHA-256, audience, grant id/reason/time,
`bridge_scope`, and every dependency's stable ref, canonical path, exact hash,
and audience-scoped restriction signature, plus exact strip targets. Unknown,
partial, duplicate, ambiguous, copied, renamed, path/ref/audience-drifted, or
alias/traversal identities SHALL fail closed. Release, scope, and purpose SHALL
only narrow the bridge's ordinary ceiling.

#### Scenario: Approval cannot transfer to a copy or audience

- **WHEN** approved bridge bytes are copied, renamed, or requested by another
  audience
- **THEN** the copy or request does not inherit the release approval

#### Scenario: Unapproved complete bridge is withheld

- **WHEN** active governance finds complete bridge metadata without an exact grant
- **THEN** it returns the `RELEASE_UNAPPROVED` admission outcome

### Requirement: Dependency snapshots use deterministic scoped signatures

Each dependency restriction signature SHALL deterministically encode the
audience, sorted live membership, scope constraints, intersecting rule id/kind/
ceiling/purpose/purpose-condition/typed canonical options, and persistent
standing grants. It SHALL NOT use a global policy fingerprint or ephemeral
session state. Relevant dependency or restriction changes SHALL stale the
release; unrelated scopes or audiences SHALL NOT.

#### Scenario: Relevant restriction change stales an approval

- **WHEN** a dependency's applicable audience rule, standing grant, membership,
  or constraint changes
- **THEN** the bridge is withheld as `RELEASE_STALE`

### Requirement: Normal authoring and reviewed release are separate

Normal remember and replacement SHALL use validate-only, reviewed draft, and
commit; bridge fields SHALL be all-or-none, source identities normalized to stable
refs, and fields bound into the draft hash/token. This commit SHALL create an
unreleased bridge only. Owner-reviewed `govern_memory propose -> commit` SHALL
separately approve or reapprove the exact bridge and record receipt/causation
evidence. Host confirmation SHALL be treated as the owner trust boundary.

#### Scenario: A committed bridge draft is not released

- **WHEN** normal authoring commits a valid bridge draft before release approval
- **THEN** the note remains withheld until the separate owner-reviewed approval

### Requirement: Admission and stripping cover every disclosure surface

The system SHALL hash the exact bytes it parses, decides, receipts, and projects
after cache lookup. One centralized admission result SHALL govern direct and
immutable reads, decide/explain/simulate, page/unit/mixed search, graph, pack,
review context, and terminal filtering. Bridge or dependency drift SHALL be
path-free `RELEASE_STALE` everywhere. A valid bridge SHALL be evaluated by its
own scope only and SHALL recursively strip approval-resolved dependency
provenance, even absent from the result pool, from bridge metadata, sources,
evidence, history, body Relations, links, raw content, graph seed/node/edge,
relation/supersession/parent/matched-unit fields, and title/path/ref plus encoded,
case, bare, and link aliases. Projection SHALL NOT mutate canonical data or
shared cache entries.

#### Scenario: Restricted provenance never escapes a released bridge

- **WHEN** a valid released bridge is returned with a dependency absent from the
  retrieval pool
- **THEN** no dependency title, path, stable ref, alias, relation, or raw
  provenance appears in the response

### Requirement: Scope constraints are narrow deterministic L2 micro-bridges

A scope-registered constraint string SHALL be emitted only when the ordinary
decision permits L2. One distinct validated provenance-free string MAY be emitted;
multiple distinct applicable strings SHALL refuse the constraint below L2. This
set-based path SHALL remain separate from legacy rule-option fallback and SHALL
not widen an ordinary decision.

#### Scenario: Ambiguous constraints are not chosen by order

- **WHEN** multiple distinct scope constraints apply
- **THEN** no constraint is emitted and the result remains below L2
