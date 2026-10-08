## Purpose

Keep managed knowledge useful within configured connector boundaries and preserve its content, identities, and provenance during offline consolidation.

## ADDED Requirements

### Requirement: One owner can use connectors with different content ceilings

The destination SHALL preserve one owner identity while enforcing independently configured connector content ceilings. Allowed content SHALL remain useful and legitimately writable. Hidden content SHALL NOT affect observations through any Exomem-mediated surface. Admission core, private vocabulary domains, and offline import SHALL remain distinct required deliveries.

#### Scenario: Admission core ships before import

- **WHEN** the connector admission batch is verified and released
- **THEN** the owner can use independently restricted connectors on existing content
- **AND** that release does not claim complete T16, private vocabulary promotion, or real consolidation

### Requirement: Vocabulary domains separate public and private definitions

Delivery B SHALL provide an explicit public core, an owner-selected public extension, and private domains tied to canonical Scope IDs. Keys, aliases, folder resolution, collisions, hashes, and promotion SHALL use admitted domains. Private definitions SHALL NOT automatically join the public registry or influence public operation outcomes.

Each registry instance SHALL combine the core with exactly one public or Scope-bound extension. Private instances SHALL NOT inherit the public extension. Registry operations SHALL select one admitted instance. Page interpretation SHALL use configured bindings and canonical membership, preserving instance identity across consumers. An unresolved multiple binding SHALL make only dependent interpretation or mutation unavailable.

#### Scenario: A private definition shares a public candidate key

- **WHEN** an owner connector proposes a key in an admitted public domain while the same key exists privately
- **THEN** public success, conflict, and hashes depend only on admitted domain definitions
- **AND** the private definition remains undisclosed

#### Scenario: A limited owner promotes an admitted definition

- **WHEN** a limited owner performs a legitimate promotion within an admitted domain
- **THEN** the existing governed authoring flow remains usable without a new human approval gate
- **AND** no private definition becomes public through automatic union

#### Scenario: Public promotion collides with a private definition

- **WHEN** the public extension adds a key, alias, or folder already used by a private extension
- **THEN** the public operation ignores that private definition and leaves its interpretation unchanged
- **AND** admitted public and private pages remain readable together under their own instances

#### Scenario: Two private bindings apply to one page

- **WHEN** canonical membership selects multiple private instances without a configured selection
- **THEN** dependent vocabulary interpretation or mutation returns an unavailable outcome
- **AND** ordinary admitted reads continue without choosing an instance by lookup success or iteration order

### Requirement: Vocabulary instance bytes and history require canonical admission

Private overlay and history paths SHALL retain their canonical Scope protection through direct reads, registry operations, and supported portability. An instance selector or filename SHALL NOT confer authority. Inspect, propose, save, history, restore, usage, and cache reuse SHALL use the selected instance and current admission. Unarmed vaults SHALL retain legacy behavior. Arming SHALL explicitly assign legacy definitions and page bindings without declaring them public automatically.

Private mutations SHALL keep reasons and before/after hashes in protected instance history without copying them into the shared operation log or its rotation archives. Public mutation logs SHALL remain useful.

#### Scenario: A denied connector directly addresses a private overlay

- **WHEN** a connector supplies the private overlay path, history path, or canonical instance selector
- **THEN** existing admission withholds the bytes before definition or history lookup
- **AND** the Scope's metadata selectors cannot leave those paths unprotected

#### Scenario: Public authoring follows a hidden-only edit

- **WHEN** private definitions or history change while admitted public content stays fixed
- **THEN** public inspect, propose, save, history, and restore retain the same outcomes and hashes
- **AND** no private cache identity, collision, or usage count enters the result

#### Scenario: A private registry save produces auxiliary history

- **WHEN** an administrator saves or restores a private extension
- **THEN** protected instance history retains the reason and version evidence
- **AND** shared log reads and rotation disclose no private path, reason, hash, or private-write count

### Requirement: Prospective vocabulary selection precedes destination derivation

Creation SHALL select one admitted prospective instance before registry lookup, folder resolution, candidate enumeration, or collision checks. Supplied metadata, independently configured destination bindings, or an explicit non-authorizing selector SHALL establish that selection. Unresolved selection SHALL make only dependent creation unavailable. The mutation boundary SHALL recheck the resulting destination and proposed membership. Moves and reclassification SHALL validate both instance interpretations and report interpretation changes without copying definitions.

#### Scenario: A type resolves to different public and private folders

- **WHEN** a new page's supplied metadata and configured destination do not select one instance
- **THEN** creation requires an explicit admitted selection or returns dependent unavailability before lookup
- **AND** lookup success or a derived destination cannot silently select the instance

### Requirement: Offline inventory accounts for every canonical object

Delivery C SHALL inventory the complete source snapshot and destination without mutating either corpus. It SHALL account for canonical content, authored history, policy, access, and review state. Fixed exclusions SHALL separate runtime, derived, and operational evidence state. Every unsupported or excluded object SHALL have an explicit disposition; nothing SHALL silently disappear.

#### Scenario: Source recall omits private content

- **WHEN** a live MCP view hides source material
- **THEN** the importer does not treat that view as a complete inventory
- **AND** it inventories verified offline canonical data instead

#### Scenario: A source contains unsupported canonical data

- **WHEN** inventory encounters an unsupported object or unresolved dependency
- **THEN** reconciliation records the object and required disposition
- **AND** import does not report completeness by dropping it

### Requirement: Reconciliation resolves identity and dependent conflicts before publication

Reconciliation SHALL distinguish exact duplicates, unique additions, identity-equivalent relocation, divergent identities, divergent normalized paths, logical duplicates, dependent structural conflicts, and nonportable authority. It SHALL bind finite resolutions to exact snapshots and before/after bytes. Unresolved dependencies SHALL prevent affected publication without silent semantic merging.

#### Scenario: Identity and path both conflict

- **WHEN** the same durable identity or normalized path names different object bundles
- **THEN** reconciliation records both identity and path consequences and affected references
- **AND** publication waits for an explicit valid resolution

#### Scenario: A unique page has an invalid dependency

- **WHEN** its target relation, unit anchor, Record identity, media pair, or history edge would become ambiguous
- **THEN** reconciliation retains the dependent conflict
- **AND** path uniqueness alone does not permit publication

#### Scenario: Logical equality differs from exact equality

- **WHEN** content matches only after removing or changing identity metadata
- **THEN** reconciliation treats it as an identity decision
- **AND** it does not perform exact-byte deduplication

### Requirement: Exact duplicate reuse preserves provenance

Reusing an exact duplicate SHALL preserve a durable protected mapping from source snapshot, object, identity, path, and hash to the destination object. A publication no-op SHALL remain represented in inventory and reconciliation evidence. Later source retention changes SHALL NOT erase that mapping.

#### Scenario: Both inventories contain the same bytes

- **WHEN** the importer reuses an existing exact destination object
- **THEN** no duplicate publication is required and both inventory origins remain represented
- **AND** protected mapping evidence survives completion and rollback

### Requirement: Import preserves canonical content and reference integrity

Import SHALL preserve Sources, Evidence, Records, media, sidecars, semantic units, stable identities, authored history, relations, citations, and review provenance. It SHALL verify every planned identity or path mapping and dependent reference. Append-only originals SHALL NOT be overwritten or body-rewritten; deduplication SHALL require exact bytes.

#### Scenario: An original collides with another path

- **WHEN** a Source or Evidence object requires relocation
- **THEN** its exact bytes and provenance remain intact at a valid collision-free destination
- **AND** planned references resolve through the recorded mapping

#### Scenario: Media and Records are imported

- **WHEN** the source contains binaries, sidecars, Record items, audit data, and semantic anchors
- **THEN** the imported canonical bundles and their references remain complete and valid
- **AND** derived media, retrieval, and graph state is rebuilt from destination canonical data

### Requirement: Source authority never becomes destination authority by copying

The importer SHALL NOT install source credentials, audiences, grants, tokens, sessions, connector mappings, runtime bindings, or derived indexes as destination authority. Source policy and review authority MAY remain protected provenance, but active destination protection SHALL use destination-owned configuration and existing governance owners.

#### Scenario: Source and destination identifiers look alike

- **WHEN** source authorization metadata resembles a destination owner or client binding
- **THEN** similarity and copied bytes confer no destination authority
- **AND** destination admission uses only its verified bindings and configured protection

### Requirement: Private staging and fingerprints bind the exact import

Source extraction, candidate bytes, and complete destination preimages SHALL remain in bounded private staging outside recall and active roots. Fingerprints SHALL bind inventoried bytes, planned effects, verified copies, and recovery targets. Missing or changed required artifacts SHALL prevent the affected effect without substituting unverified bytes.

#### Scenario: A staged object changes after inventory

- **WHEN** its current bytes no longer match the recorded fingerprint
- **THEN** import refuses the affected publication
- **AND** a prior inventory result does not authorize the changed object

#### Scenario: Ordinary recall runs before maintenance

- **WHEN** private source staging and import state already exist
- **THEN** ordinary recall does not index, count, or return them
- **AND** staging never creates a pre-admission release path

### Requirement: Offline publication and recovery preserve exact state

Import SHALL use stopped and drained maintenance and existing mutation/restore owners. It SHALL verify complete preimages and destination protection before content publication. Exact retries SHALL resume recorded effects without semantic replay. Mixed state SHALL remain unserved. Abort and rollback SHALL preserve source bytes, later work, and append-only evidence.

#### Scenario: A process dies after publication

- **WHEN** recorded fingerprints prove an effect committed but acknowledgement is missing
- **THEN** recovery completes its evidence without applying the effect again
- **AND** changed retry identity or payload cannot adopt the operation

#### Scenario: A completed destination has later edits

- **WHEN** rollback inventories a destination changed after import
- **THEN** every later create, edit, move, and delete receives an explicit preservation treatment
- **AND** restoration never silently overwrites that work

### Requirement: Rollback and retirement never remove the last imported copy

Before a destructive rollback or source retirement, current copy evidence SHALL prove a surviving verified copy of every imported content and provenance bundle. A pre-import preimage SHALL NOT count as an imported copy. Retiring the last source/archive copy SHALL require a retained forward copy and exclude rollback targets that lose imported bundles.

#### Scenario: A rollback would remove the final bundle

- **WHEN** neither a retained source, archive, destination treatment, nor forward snapshot preserves an imported bundle
- **THEN** the destructive operation refuses before changing bytes
- **AND** stale or unavailable copy evidence cannot count as a survivor

#### Scenario: Source retirement changes recovery options

- **WHEN** separately authorized retirement makes source-only or archive-only data irrecoverable
- **THEN** the operator receives the exact retained-versus-irrecoverable statement and verified surviving-copy evidence
- **AND** later rollback preserves all imported bundles rather than returning to a lossy pre-import state

### Requirement: Real import and cutover require operational authority

Reusable capability proof SHALL remain separate from real import, connector routing changes, and source retirement. A real operation SHALL use fresh source/destination fingerprints and verified destination connector behavior. Rehearsal SHALL use disposable copies and prove rollback. Destructive source action SHALL require its own explicit authority and current copy evidence.

#### Scenario: Disposable rehearsal succeeds

- **WHEN** import, allowed utility, hidden-content isolation, and rollback pass on disposable copies
- **THEN** the result supplies rehearsal evidence only
- **AND** it performs no real import, connector cutover, source deletion, key destruction, or account change
