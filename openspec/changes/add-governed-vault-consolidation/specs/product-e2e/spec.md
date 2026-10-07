## ADDED Requirements

### Requirement: Authenticated twin-vault workflows prove admission and utility

Delivery A acceptance SHALL exercise real local/OAuth authentication on temporary twin vaults with identical allowed data and different protected canaries. Two connectors SHALL retain the same owner identity and different ceilings. Tests SHALL prove both useful allowed operations and independence from hidden content across affected surfaces.

#### Scenario: Allowed recall and writes remain useful

- **WHEN** both owner connectors read, search, recall, and edit content admitted by their ceilings
- **THEN** expected allowed results and legitimate mutations succeed
- **AND** the narrower connector's hidden corpus cannot change its returned observations

#### Scenario: Hidden canaries span producer families

- **WHEN** protected canaries differ in rows, media, graph edges, history, aliases, citations, registries, and provenance
- **THEN** paired checks cover ranking, graph, canonical state, bootstrap, aggregates, receipts, and errors
- **AND** hidden-target mutations leave content unchanged and reveal no stored existence facts

#### Scenario: A hidden contributor appears where none existed

- **WHEN** a hidden sibling is added without changing admitted contributors
- **THEN** admitted inspection, hashes, guards, audit status, and write outcomes remain independent of its presence
- **AND** a zero hidden-contributor count cannot enable a whole-corpus observation

#### Scenario: Hidden rows change physical allocation

- **GIVEN** vaults contain identical admitted canonical rows
- **WHEN** hidden rows precede or follow those rows in storage
- **THEN** canonical snapshots, query results, and inspection guards remain identical at the same observation time
- **AND** SQL allocation and hidden history do not contribute to the limited snapshot

#### Scenario: Configured capture preserves namespace visibility

- **WHEN** limited owners capture admitted pages and byte-bound artifacts in configured destinations
- **THEN** capture succeeds and reports its actual destination
- **AND** unrestricted writers cannot make the namespace private through replacement or companion metadata changes

### Requirement: Point-write acceptance proves isolated guards and retries

After the actual S1 successor merges, acceptance SHALL exercise current-checkout authenticated point writes against paired admitted rows and hidden siblings. It SHALL cover hidden key/hierarchy conflicts, held candidates, shared caller retry IDs, complete-field admission, and historical receipt projection. The new migration SHALL prove versioning, actual old-runtime refusal, restore/rollback, and query-plan behavior against the post-S1 schema.

#### Scenario: Hidden transactions reuse a caller retry ID

- **WHEN** different verified clients and targets use the same supplied retry ID while private transactions change
- **THEN** scoped success, conflict, and receipt observations remain independent
- **AND** legacy IDs cannot occupy or authorize the new scoped column

#### Scenario: A response is lost after a successful point write

- **WHEN** the client retries after public or hidden rows change, including after a valid session refresh
- **THEN** the original argument digest selects the committed historical envelope after current admission
- **AND** replay neither compares the old guard with current state nor repeats the mutation

#### Scenario: A legacy success or held refusal is retried

- **WHEN** the old request reaches the new point domain with its unchanged old guard
- **THEN** it cannot replay a legacy terminal or create a duplicate held candidate
- **AND** no per-ID legacy lookup reveals whether that terminal exists

#### Scenario: Contributor authority changes before publication or replay

- **WHEN** target fields, contributor membership, session authority, or connector configuration narrows
- **THEN** precommit checks or historical receipt projection enforce the current authority
- **AND** private Markdown-log projection stays pending without replacing hidden rows

### Requirement: Session and cache acceptance uses live authority changes

Tests SHALL prove current connector configuration and originating authentication at result and capability consumption. They SHALL cover re-registration, revocation, generation, expiry, refresh-family invalidation, governance session attachment, signed v3 delegation, legacy v2 transfer, and cache reuse without replacing the real authority with caller claims.

#### Scenario: Authority changes after capability minting

- **WHEN** the fixture narrows configuration or invalidates the originating authentication before consumption
- **THEN** cached and delegated results enforce current authority
- **AND** the original signature or owner label cannot preserve earlier access

#### Scenario: A client registers again

- **WHEN** the fixture authenticates a new client registration with the same display name
- **THEN** the new binding receives the restrictive default until explicitly configured

### Requirement: Actual older runtimes prove compatibility refusal

Acceptance SHALL execute an actual pre-capability runtime against an armed root and an actual version-1-only verifier against an armed version-2 archive. It SHALL NOT substitute a patched capability catalogue. The old archive verifier SHALL reject before extraction, and the old runtime SHALL reject without guard deletion.

#### Scenario: Both downgrade paths are attempted

- **WHEN** the real older runtime receives the armed root and the older verifier receives its supported export
- **THEN** both refuse at their compatibility boundary
- **AND** no content is served or extracted under unrestricted legacy behavior

### Requirement: Portability acceptance proves fresh-state protection

Acceptance SHALL round-trip an armed version-2 archive into a new root and external state directory. It SHALL prove required compatibility, destination configuration, and current admission after restart. A separate unarmed version-1 round-trip SHALL remain successful. Source connector authority SHALL never be installed.

#### Scenario: Restored destination initially lacks configuration

- **WHEN** an armed archive restores successfully but destination host configuration is absent
- **THEN** startup remains unserved
- **AND** supplying compatible destination configuration enables only its configured connectors

#### Scenario: Protected restore is interrupted at canonical publication boundaries

- **WHEN** legacy and enrolled v4 destinations fail around Scope commit, receipt intent/terminal, or workspace mirror publication
- **THEN** exact retry preserves protected bytes and append-only history through existing recovery owners
- **AND** process death after rename is tested separately from an exception that rolls back

#### Scenario: A retry changes identity or injects staging residue

- **WHEN** archive, payload, operation, destination, Scope mirrors, event evidence, durable head, or destination authority no longer matches
- **THEN** retry refuses without overlaying live content or discarding evidence
- **AND** valid no-op rebuild and unarmed version-1 restore remain successful

### Requirement: Private vocabulary acceptance precedes real import

Delivery B SHALL prove domain-scoped resolution, collisions, folders, aliases, hashes, and promotion against paired private definitions. Public core and owner-selected public extensions SHALL remain explicit. Delivery A SHALL NOT claim this proof or complete T16 while private vocabulary domains remain absent.

#### Scenario: Hidden definitions change during public authoring

- **WHEN** two vaults differ only in private vocabulary definitions
- **THEN** equivalent allowed public authoring has the same success, conflict, and returned hash behavior
- **AND** private definitions neither join the public registry nor suppress allowed promotion

### Requirement: Offline import acceptance proves complete preservation and recovery

Delivery C SHALL use disposable managed vaults to prove complete inventory, conflict reconciliation, exact duplicate provenance, canonical integrity, source immutability, and no copied authority. Process interruption and exact retries SHALL prove recovery and rollback without deleting the last imported copy or overwriting later work.

#### Scenario: Import and rollback complete on disposable copies

- **WHEN** the offline fixture imports representative canonical content and then rolls back
- **THEN** fingerprints, references, copy evidence, and append-only receipts match the declared result
- **AND** protected content remains hidden through real authenticated connectors

#### Scenario: Later work or a missing copy blocks destructive restoration

- **WHEN** a rollback would overwrite later edits or remove the last verified imported bundle
- **THEN** the operation leaves those bytes intact pending reconciliation
- **AND** its outcome names the incomplete state instead of claiming success

### Requirement: Product evidence does not authorize operational cutover

Tests SHALL use temporary isolated state without external provider calls or live writes. Full completion verification and independent review SHALL cover the exact delivered batch. Reports SHALL distinguish Delivery A, Delivery B, Delivery C, real import, cutover, and source retirement; incomplete or unavailable proof SHALL remain visible.

#### Scenario: Delivery A passes acceptance

- **WHEN** connector admission and portability checks pass and the batch ships
- **THEN** evidence records Delivery A completion only
- **AND** private vocabulary domains, offline import, and separately authorized operational cutover remain required
