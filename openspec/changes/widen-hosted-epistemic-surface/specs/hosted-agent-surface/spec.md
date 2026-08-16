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
