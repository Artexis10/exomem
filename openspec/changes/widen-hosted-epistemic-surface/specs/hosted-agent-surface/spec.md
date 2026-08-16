## ADDED Requirements

### Requirement: Hosted Epistemic Profile Exposes The Complete Loop

The system SHALL define the immutable profile `hosted-alpha-agent-v3` in the canonical surface-profile registry. The profile MUST contain exactly the pinned `hosted-alpha-agent-v2` membership — `bootstrap`, `ask_memory`, `read_memory`, `browse_memory`, `remember`, `observe_memory`, `capture_source`, `compile_source`, `preserve_evidence`, `review_memory`, `review_item_context`, `triage_memory`, `connect_memory`, `record_memory` — in that order, followed by `replace_memory`, `plan_memory`, and `edit_memory`. Every member MUST be a Tier-1 product command exposed on the `rest` surface, and changing membership MUST require a new profile identifier.

#### Scenario: Epistemic profile is selected

- **WHEN** a caller resolves `hosted-alpha-agent-v3` for the REST-backed Hosted agent surface
- **THEN** the resolver returns exactly the seventeen named Tier-1 product commands in that pinned order
- **AND** every returned schema, description, route, and read/write classification comes from the corresponding canonical command entry rather than a profile-local copy

#### Scenario: Belief revision, intent, and correction are reachable

- **WHEN** the `hosted-alpha-agent-v3` membership is inspected
- **THEN** it contains `replace_memory`, `plan_memory`, and `edit_memory`
- **AND** a Hosted agent bound to this profile can supersede a conclusion, state a durable intent, and correct a page in place without leaving the profile

#### Scenario: Widening does not reach Tier-2 or broad administration

- **WHEN** the `hosted-alpha-agent-v3` membership is inspected
- **THEN** it still excludes coordination internals, transfer, media processing, adoption, maintenance, schema administration, and every Tier-2 command
- **AND** those exclusions cannot be bypassed by selecting another surface or enabling Tier-2

### Requirement: Hosted Agents Cannot Rewrite Governed Schema Or Policy

A Hosted surface profile that exposes a broad page-mutation command SHALL refuse any invocation whose caller-supplied write target names the governed schema tree (`_Schema`) or the policy tree (`_Governance`). The refusal MUST happen at the hosted command boundary, before lifecycle admission and before the command leaf, and MUST return a stable machine-readable error.

**The guard and the command leaf MUST NOT hold independent notions of the same path.** The guard SHALL derive the target it judges by calling the same normalisation the leaf calls to decide which file to open — one shared function, invoked by both, not a copy of one in each. A guard that re-implements the leaf's normalisation is non-conformant even while it happens to agree, because the defect is the divergence, not any particular disagreement. In particular, the normalisation MUST supply the governed-folder prefix when the caller omitted it, since the leaves do, and a guard that joins at a different root evaluates a path the leaf never touches.

Matching MUST apply both of the following readings to every candidate target, and MUST refuse if *any* reading names a protected tree:

- A **spelling** reading, per path component, case-insensitive, and insensitive to trailing dots and spaces because the target platform strips them before the filesystem sees the component. The reading MUST be correct for every platform the cell can be deployed on rather than for the authoring platform alone, so drive-qualified, UNC, and extended-length spellings MUST be segmented as the deploying platform would segment them.
- A **resolution** reading, which resolves the deepest ancestor of the target that exists on disk and tests containment against the resolved protected tree roots, then applies the spelling reading to the unresolved remainder. Resolution is what expands an OS-level alias, junction, or link whose name is not a protected tree name; resolving the deepest *existing* ancestor is what keeps that working when the target itself does not exist yet, which is the normal case for creating a new document.

The guard MUST NOT fail open. Any error while evaluating any reading — parsing, resolution, filesystem, or an argument that cannot be read at all — MUST result in refusal, never in an allow and never in falling through to an answer derived from the remaining readings alone.

Where a guarded command exposes more than one caller-supplied target argument, every such argument MUST be guarded. Partial coverage of a guarded command SHALL NOT be treated as coverage.

This requirement replaces the protection that `hosted-alpha-agent-v1` obtained from *not exposing* `edit_memory` or `replace_memory`, recorded in that profile's own requirement as "the command is absent from the profile and rejected before invocation or lifecycle admission" for a path under `_Schema`. Profile absence SHALL NOT be relied upon as the control once a profile exposes those commands.

The guard SHALL be scoped to Hosted surface profiles. Local, CLI, and MCP surfaces on a single-user vault MUST retain the ability to customise that vault's own `_Schema`, and reads of either tree MUST remain unaffected.

Every mutating command a Hosted profile exposes MUST be classified either as guarded or as constrained by its own command leaf, and the cell MUST refuse to serve a profile with an unclassified mutation. Recording a command as leaf-constrained SHALL NOT be a way to wave it through: every caller-supplied argument that can shape where such a command writes MUST be shown to leave both protected trees byte-identical and unchanged in membership, without the guard firing. The set of arguments so checked MUST be derived from the command's published schema rather than from a hand-maintained list, so an argument added later is covered by default rather than certified by omission.

#### Scenario: A hosted agent tries to rewrite the schema tree

- **WHEN** a Hosted profile exposing `edit_memory` or `replace_memory` receives an invocation whose write target is inside `_Schema`
- **THEN** the cell refuses it with a stable error before lifecycle admission and before the command leaf
- **AND** no byte of the targeted document changes

#### Scenario: A hosted agent tries to rewrite the policy tree

- **WHEN** the same invocation targets a document inside `_Governance`
- **THEN** it is refused identically

#### Scenario: The refusal is probed for an escape

- **WHEN** the target is expressed with a differently cased tree segment, a `..` traversal, a leading `./`, doubled or backslash separators, a trailing separator, or a deeper nested path
- **THEN** every form is refused with the same stable error

#### Scenario: The target omits the governed-folder prefix

- **WHEN** the target is written without the governed-folder prefix, which the command's own description invites, so the leaf will supply it before opening the file
- **THEN** the guard judges the same prefixed target the leaf will open, rather than a path rooted somewhere the leaf never looks

#### Scenario: The target reaches a protected tree without naming it

- **WHEN** the target reaches `_Schema` or `_Governance` through an operating-system alias, junction, or link, so no component of the text is a protected tree name
- **THEN** it is refused with the same stable error, because the deepest existing ancestor of the target is resolved and tested for containment
- **AND** this holds whether or not the target itself exists yet

#### Scenario: A component carries trailing dots or spaces

- **WHEN** a component of the target is spelled with trailing dots or spaces, which the target platform strips before the filesystem sees it
- **THEN** it is refused identically to the bare component

#### Scenario: The target is drive-qualified or UNC

- **WHEN** the target is spelled with a drive prefix, a UNC root, or an extended-length prefix
- **THEN** it is segmented as the deploying platform would segment it, and a protected component in it is refused

#### Scenario: The target is absolute-shaped

- **WHEN** the target begins with one or more `/` or `\` separators — whether it resolves inside the vault, outside it, or nowhere at all
- **THEN** it is refused with the same stable error, because the write leaf would read it as vault-relative
- **AND** a failure to parse or resolve any one interpretation does not cause the guard to allow the invocation

#### Scenario: Ordinary governed pages and reads are unaffected

- **WHEN** the same command targets an ordinary compiled page, or any command reads from a protected tree
- **THEN** the guard does not fire and the request proceeds to its normal handling
- **AND** a local single-user surface may still customise its own `_Schema`

#### Scenario: A profile exposes an unclassified mutation

- **WHEN** a Hosted profile exposes a mutating command that is neither covered by the protected-tree guard nor recorded as constrained by its own command leaf
- **THEN** the cell refuses to serve that profile rather than exposing an unguarded write primitive

#### Scenario: A leaf-constrained classification is checked rather than trusted

- **WHEN** a mutating command is recorded as constrained by its own command leaf
- **THEN** every argument in its published schema that could shape a write target is probed against a protected tree, and both protected trees are left byte-identical and unchanged in membership
- **AND** the refusal does not come from the protected-tree guard, which would mean the classification was wrong

#### Scenario: The guard cannot read an argument

- **WHEN** evaluating a target raises at any point — an unreadable value, a resolution error, or a filesystem error
- **THEN** the invocation is refused

#### Scenario: A guarded command carries a second target argument

- **WHEN** a guarded command exposes more than one caller-supplied target argument
- **THEN** each of them is guarded, and a protected tree named through any of them is refused identically

### Requirement: A Widened Profile Is Additive And Never Mutates A Published Profile

Adding a Hosted profile SHALL NOT change the membership, pinned order, generated package bytes, package lock, or recorded release identity of any already-published profile. A new profile MUST be introduced as a new registry entry and a new candidate package; an existing profile MUST NOT be relabelled, reordered, or extended in place.

#### Scenario: Existing profiles are re-resolved after a widening

- **WHEN** `hosted-alpha-agent-v1` and `hosted-alpha-agent-v2` are resolved after `hosted-alpha-agent-v3` is registered
- **THEN** each returns its original command list in its original order
- **AND** the committed generated artifacts and locks for both still match a fresh render

#### Scenario: A published release identity is re-verified

- **WHEN** the recorded v1 release-identity digests are checked after the widening
- **THEN** every pinned artifact still hashes to its recorded value

### Requirement: Profile Membership Is Not The Local Tool Surface

Hosted surface-profile membership SHALL be an exposure policy over the canonical product command registry and MUST NOT change the full local server's generated tool surface. Adding, removing, or reordering commands within a Hosted profile MUST leave the packaged local tool-surface contract and its recorded digest unchanged.

#### Scenario: A profile is added

- **WHEN** a new Hosted surface profile is registered without changing any canonical command
- **THEN** the packaged local MCP tool-surface contract and its digest are unchanged
- **AND** no external connector registration or plugin fingerprint is invalidated by the profile addition alone

### Requirement: A Records-Bearing Candidate Binds The Records Reader Floor

Every Hosted client-plugin candidate whose profile exposes `record_memory` SHALL bind `minimum_records_reader_version: 2` in its compatibility descriptor and package lock, SHALL bind the digest of its own committed records selection cases, and SHALL be held to the records live-acceptance expectations for its own profile identifier before promotion. A candidate MUST NOT satisfy those obligations by referencing another candidate's committed files.

#### Scenario: A second records-bearing candidate is rendered

- **WHEN** a candidate other than `hosted-alpha-agent-v2` exposes `record_memory` and is rendered
- **THEN** its compatibility descriptor and package lock pin reader floor 2 and the digest of its own selection cases
- **AND** its promotion path validates live acceptance against its own profile identifier

#### Scenario: A records-bearing candidate is promoted without acceptance evidence

- **WHEN** promotion is attempted for a records-bearing candidate without closed live acceptance evidence
- **THEN** promotion fails closed
- **AND** the committed promotion record remains unpromoted
