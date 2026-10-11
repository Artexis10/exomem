## ADDED Requirements

### Requirement: Armed exports use a version that older readers refuse

An armed export SHALL use manifest schema version 2 with a mandatory portable armed requirement. New readers SHALL accept versions 1 and 2. Unarmed exports SHALL remain version 1. The reserved armed artifact SHALL have an explicit export classification, and its requirement SHALL agree with the manifest.

#### Scenario: An older reader opens an armed export

- **WHEN** an actual version-1-only verifier receives an armed version-2 archive
- **THEN** it rejects the manifest before extraction
- **AND** an optional extra field on a version-1 manifest cannot substitute for this refusal

#### Scenario: An unarmed vault is exported

- **WHEN** no connector ceiling has been armed
- **THEN** the export retains the existing version-1 format and supported round-trip behavior

#### Scenario: Manifest and armed artifact disagree

- **WHEN** an archive omits, alters, or contradicts its mandatory armed requirement
- **THEN** verification refuses before publication
- **AND** excluded internal-state defaults cannot hide the missing protection

### Requirement: Restore preserves protection without transplanting connector authority

Supported backup, export, and restore SHALL preserve the armed requirement in fresh external state. The artifact SHALL carry referenced protective Scope definitions from active canonical policy and a fingerprint of normalized selectors. Whole-vault export SHALL remain unavailable to content-limited callers. A restored destination SHALL remain unserved until its host configuration satisfies the requirement. Restore SHALL install exact definitions through canonical Scope and publication owners under stopped destination maintenance authority. Runtime membership SHALL use only canonical policy Scopes. Source rules, credentials, connector mappings, audiences, grants, sessions, custody, and runtime authority SHALL NOT become destination authority.

#### Scenario: An armed archive is restored into fresh state

- **WHEN** restore targets a new root and external state directory
- **THEN** required compatibility and the portable armed requirement survive
- **AND** service remains unavailable until destination host configuration satisfies the required protection

#### Scenario: The destination configures different clients

- **WHEN** the destination host supplies its own valid connector configuration
- **THEN** serving uses the destination's verified client bindings
- **AND** the archive confers no source connector or administrative authority

#### Scenario: The source policy workspace differs from active policy

- **WHEN** an armed export preserves protective Scopes
- **THEN** it reads definitions from the active canonical policy owner
- **AND** pending authoring files cannot replace the active selectors

#### Scenario: The destination has a conflicting Scope identity

- **WHEN** a portable protective Scope shares an existing destination ID with different selectors
- **THEN** restore refuses without replacing the destination definition
- **AND** an explicit destination policy amendment is required before that identity can change

#### Scenario: Restored content violates a capture namespace

- **WHEN** restored content would place privately admitted metadata inside a configured capture namespace
- **THEN** restore refuses before serving or publishing an eligible capture destination
- **AND** location never overrides that content's protective membership

### Requirement: Protected restore retries preserve exact destination evidence

Restore retries SHALL preserve destination-owned Scope mirrors and append-only receipt evidence. Strict staging verification SHALL accept only exact canonical mirrors and residue verified against the actual destination authority, durable head, and exact restore proposal. It SHALL retain archive integrity checks and refuse unexplained extras without deleting them. A pending mutation marker SHALL require its canonical validator and the original linked operation. Hosted and standalone restore SHALL share durable journal mechanics. Before rename, the journal SHALL reserve the proposal ID. Before issuance, it SHALL persist the exact missing documents.

#### Scenario: Protected publication fails after Scope installation

- **WHEN** a failure follows Scope installation on the published destination
- **THEN** restore retains the destination inode and verifies its evidence against original destination authority before resuming the exact proposal
- **AND** existing Scope presence cannot bypass incomplete receipt or mirror recovery

#### Scenario: A process dies after publication rename

- **WHEN** the exact request retries against a live tree left by process death
- **THEN** existing durable recovery evidence must bind the archive, manifest, operation, destination, protected documents, and canonical bytes
- **AND** an unbound or changed request retains the no-overlay refusal

#### Scenario: Staging contains invented protection or evidence

- **WHEN** Scope bytes differ, an event chain lacks destination authority, or unrelated governance files appear
- **THEN** verification refuses without deleting, truncating, or adopting that residue
- **AND** a locally valid event chain alone cannot prove destination ownership

#### Scenario: Protected publication retains its attachment

- **WHEN** protected publication fails after its destination inode becomes bound to authority
- **THEN** restore preserves the tree and destination authority in place without returning the inode to staging
- **AND** an exact retry uses its durable journal without replacing unrelated roots

#### Scenario: Physical relocation stops after source deletion

- **WHEN** an exact retry finds the destination-bound migration manifest in progress
- **THEN** it verifies canonical archive bytes and lets the existing migration owner resume its verified portable members
- **AND** complete continuity checks every portable member at final placement before protected publication completes
- **AND** altered portable bytes refuse without discarding the destination inode, state, or evidence

#### Scenario: Existing destination authority refuses replacement

- **WHEN** restore admission finds unavailable custody, activated vocabulary authority, or a substituted attachment
- **THEN** publication refuses before replacing the destination
- **AND** exact retry confers no authority to relocate or replace that attachment

#### Scenario: A protected hosted restore retries

- **WHEN** hosted publication bypasses the standalone publisher
- **THEN** it reserves and resumes the same protection proposal through the shared journal owner
- **AND** readiness follows complete destination protection recovery

#### Scenario: An optional rebuild alters protected restore bytes

- **WHEN** optional derived rebuilding changes canonical archive bytes during armed restoration
- **THEN** restore reports a canonical integrity violation and does not report readiness
- **AND** it retains changed bytes, destination protection, receipt evidence, and migrated portable state without destructive archive repair

### Requirement: Protective restoration proves complete archive continuity

Exact stopped protective restoration SHALL bind the complete verified archive inventory and manifest to destination identity, state placement, operation, and exact protective documents. The existing archive and residue owners SHALL verify every included configuration byte. An internally validated restore binding SHALL select versioned continuity evidence through proposal creation, commit, and recovery. Ordinary and legacy membership proposals SHALL retain their original evidence semantics.

#### Scenario: A full scaffold requires protective restoration

- **WHEN** an armed archive contains user-owned YAML configuration without admission descriptors
- **THEN** exact protective restoration verifies complete archive continuity without inventing empty membership
- **AND** its proposal marks membership consequences unevaluated rather than reporting a measured count

#### Scenario: Configuration changes during interrupted restoration

- **WHEN** a configuration byte changes, an unexplained file appears, or the destination binding changes
- **THEN** continuation refuses without discarding the changed bytes or append-only evidence
- **AND** unchanged full-scaffold restoration preserves every included configuration byte

### Requirement: Armed roots refuse unsupported runtime downgrade

An armed root SHALL retain the required connector-ceiling capability under the existing durable compatibility owner. A runtime without that capability SHALL refuse the root. Product rollback SHALL retain the guard while restoring physical isolation. Supported portability SHALL preserve this guarantee; arbitrary filesystem copies and historic binaries are outside it.

#### Scenario: An older binary opens the same armed root

- **WHEN** the actual pre-capability runtime opens the armed root
- **THEN** its existing compatibility check rejects the unknown required capability
- **AND** the proof changes neither its catalogue nor the root's guard

### Requirement: Managed-vault intake is bounded and private

Delivery C SHALL reuse existing archive version, size, entry-digest, traversal, link, duplicate-path, case-collision, and unsupported-entry checks. Extraction SHALL remain outside recall and active roots. The input archive SHALL remain immutable. A filtered live MCP crawl SHALL NOT stand in for a complete source inventory.

#### Scenario: A valid archive is staged for reconciliation

- **WHEN** a supported archive passes bounded validation
- **THEN** inventory reads verified private staging without publishing over the active destination
- **AND** the source archive remains byte-identical

#### Scenario: An archive contains unsafe or runtime entries

- **WHEN** an archive contains unsafe paths, links, duplicate normalized entries, credentials, or source-derived indexes
- **THEN** intake refuses before destination publication
- **AND** accepted authored data never grants source runtime authority

### Requirement: Import and retirement use current copy evidence

A source snapshot SHALL bind the bytes inventoried for import. A source that resumes writes SHALL require a fresh comparison before real cutover. Import completion SHALL NOT imply source retirement. Any separately authorized retirement or rollback SHALL verify surviving copies of every imported bundle before destructive effects.

#### Scenario: The source changes after snapshot creation

- **WHEN** the source resumes writes and its current fingerprint differs before real cutover
- **THEN** the operator reconciles a fresh snapshot
- **AND** an old archive does not prove the current source is unchanged

#### Scenario: The final source copy is proposed for retirement

- **WHEN** retirement would remove the last source or archive copy
- **THEN** a verified retained forward copy must preserve every imported bundle and its provenance
- **AND** a pre-import destination preimage does not count as a copy of imported data
