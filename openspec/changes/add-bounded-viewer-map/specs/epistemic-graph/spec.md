# Global viewer map delta

## ADDED Requirements

### Requirement: Global map obeys the current graph disclosure contract
The system SHALL expose a read-only global page map through the canonical and product command registries. Pages, edge authors and endpoints SHALL pass the current caller's admission and disclosure rules before counting or ranking. Semantic-unit endpoints SHALL resolve to their owning pages. Relation vocabulary SHALL preserve each source instance's admitted meaning. The operation SHALL NOT change seeded graph context, activation or canonical memory.

#### Scenario: Hidden content does not influence the visible map
- **WHEN** a page and its authored edges are hidden from the caller and inspection completes
- **THEN** removing that page entirely produces the same visible counts, ranking and map

#### Scenario: Two instances use the same relation spelling
- **WHEN** drawn edges come from two admitted instances that assign different meanings to one spelling
- **THEN** the response retains both instance and definition identities without merging their meanings

### Requirement: Map presentation retains source metadata
Returned pages SHALL expose their stored type, origin date and updated date, with null for missing values.
Drawn vocabulary SHALL expose an optional registry-owned presentation role independently of the definition key, family, description and display label.
The role SHALL NOT alter relation meaning, admission, ranking or disclosure. Unknown roles SHALL remain valid metadata for a neutral renderer fallback.
The registry SHALL preserve presentation metadata through normal load, save and generic entry conversion.

#### Scenario: A renderer sees a new role
- **WHEN** a registry defines a presentation role that the renderer does not implement
- **THEN** the map preserves that role and its source identity without guessing from a relation name

#### Scenario: Presentation changes without semantic changes
- **WHEN** an owner changes only a relation's presentation role
- **THEN** the registry preserves its key, meaning fields and aliases

### Requirement: Global map reports exact and partial coverage separately
The map SHALL include admitted isolated pages and rank by admitted incoming cross-page edges with a deterministic key tie-break. A complete inspection SHALL report exact eligible totals. A stopped inspection SHALL return partial state, nullable exact totals, observed counts and provisional ranking. Output caps SHALL report actual drawn counts and truncation separately. Unavailable graph evidence SHALL NOT appear as an empty vault.

#### Scenario: Only the output is capped
- **WHEN** the scan completes but eligible pages exceed the drawing limit
- **THEN** exact eligible totals remain available and drawn counts reflect only returned items

#### Scenario: Inspection stops early
- **WHEN** the work or time budget expires before inspection completes
- **THEN** exact totals are unknown, ranking is provisional and the response identifies partial coverage

#### Scenario: The snapshot is unavailable
- **WHEN** current graph evidence is missing, stale, unproven or incompatible
- **THEN** the map returns unavailable state with a bounded reason and no fabricated zero total

### Requirement: Global map bounds inspection and transport work
The operation SHALL bound inspection time, inspected pages and edges, and returned pages and edges. It SHALL read one validated graph snapshot without a cold whole-vault proof, filesystem census or full-graph materialization. Returned keys SHALL retain the graph's identities and include persistent memory references when available, without claiming path keys survive renames.

#### Scenario: Live projection evidence disappears
- **WHEN** the live checkpoint is missing or changes while the snapshot opens
- **THEN** the operation returns bounded unavailable state without a disk census or weaker snapshot validation

#### Scenario: A current snapshot belongs to an earlier process
- **WHEN** the existing source proof validates an inherited snapshot and a resolver exists at its matching current live projection
- **THEN** the operation reuses that resolver without requiring the stored and live checkpoint lineages to match

#### Scenario: Resident interpretation evidence is missing
- **WHEN** the validated snapshot has no resident resolver at its matching current live projection
- **THEN** edge coverage is partial, exact totals are unknown and ranking is provisional without a cold resolver build

#### Scenario: A large vault exceeds the operation's budget
- **WHEN** a large synthetic vault cannot be fully inspected within the configured budget
- **THEN** the call returns a bounded partial result within the transport deadline

#### Scenario: Hidden rows precede the admitted page or edge limit
- **WHEN** hidden rows occur before admitted rows in snapshot order
- **THEN** only admitted items consume semantic inspection limits and an extra admitted item establishes overflow

#### Scenario: An admitted author exceeds preparation bounds
- **WHEN** uniform source, summary or preparation limits interrupt an admitted author
- **THEN** its replacement edges are neither cached nor drawn and exact totals remain unknown in a partial result

#### Scenario: A changed source needs fresh derivation
- **WHEN** an admitted author changed since its resident summary
- **THEN** bounded parser and graph checkpoints prepare it without a whole-vault source census or cold resolver build

#### Scenario: A current map is redrawn
- **WHEN** the same current snapshot is read with the same caller and limits
- **THEN** returned items retain stable identities and deterministic ordering
