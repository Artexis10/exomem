## ADDED Requirements

### Requirement: Compiled writes may return one advisory structural-promotion suggestion

When a compiled-note mutation commits, the system SHALL evaluate whether the written page now carries recurring durable material outside its own declared scope. When it does, the successful result SHALL include at most one `structure_suggestion`, carried on the existing commit result and projected into the default compact terminal, for every compiled writer: `remember`, `edit_memory`, `observe_memory`, and `replace_memory`.

The suggestion SHALL report `kind`, a `strength` of exactly `strong` or `moderate`, a deterministically ordered list of reason codes, the count of off-scope durable units, and a bounded list of the recurring terms that formed the group. It SHALL NOT report a numeric confidence, score, probability, or any other continuous quantity.

The suggestion is advisory. It SHALL NOT add a value to the closed set of keys a client branches on for mutation outcome, and it SHALL NOT alter `status`, `mutated`, `path`, `warnings_count`, mutation identity, or replay behaviour. When no condition is detected the key SHALL be absent rather than null or empty.

#### Scenario: A diverged compiled page returns a suggestion in the default response

- **WHEN** a compiled page whose declared identity describes one subject accumulates recurring durable units describing a materially different subject, and a further compiled write commits
- **THEN** the write succeeds with its existing committed terminal unchanged
- **AND** the default compact response carries one `structure_suggestion` with `strength` of `strong` or `moderate`
- **AND** the suggestion names at least two reason codes in deterministic order
- **AND** the response reports no numeric confidence for the suggestion

#### Scenario: Every compiled writer can carry the suggestion

- **WHEN** the same diverged page is mutated through `remember`, `edit_memory`, `observe_memory`, or `replace_memory`
- **THEN** each committed result can carry the suggestion
- **AND** the suggestion has the same shape regardless of which writer produced it

#### Scenario: A coherent page returns no suggestion

- **WHEN** a compiled write commits to a page whose durable units remain within its declared scope
- **THEN** the response contains no `structure_suggestion` key

### Requirement: Structural detection is conservative and never triggered by size

The system SHALL require convergent evidence before emitting a suggestion. It SHALL emit nothing on a single signal, and it SHALL reserve `strong` for the case where every reason code holds.

Recurrence SHALL be established over durable semantic units rather than over write events, and the reported reason codes SHALL describe units accordingly. A term SHALL contribute to a group only when it recurs across more than one off-scope unit.

Raw page length, byte size, unit count alone, section count, and category variety SHALL NOT be sufficient to emit a suggestion. A long page whose durable units remain within its declared scope SHALL produce no suggestion regardless of its size.

#### Scenario: A long coherent research note stays quiet

- **WHEN** a compiled write commits to a large note containing many durable units, many categories, and many sections that all remain within one declared subject
- **THEN** no suggestion is emitted
- **AND** the outcome does not depend on the page's length

#### Scenario: One or two tangents do not trigger promotion

- **WHEN** an otherwise coherent page contains one or two durable units outside its declared scope
- **THEN** no suggestion is emitted, because no term recurs across enough off-scope units to form a group

#### Scenario: A single signal is insufficient

- **WHEN** fewer than two reason codes hold for a page
- **THEN** no suggestion is emitted

### Requirement: Structural detection is scoped to compiled knowledge

The system SHALL evaluate only compiled note pages. Sources, evidence, media artifacts, structured collections, planning items, navigational pages, and any page ineligible for recall SHALL NOT be analysed and SHALL NOT produce a suggestion.

A page whose declared identity announces deliberate breadth SHALL NOT be nagged toward further division. A page carrying the established hub or snapshot convention SHALL be excluded from analysis.

#### Scenario: Source and evidence artifacts never enter the advisory path

- **WHEN** a write commits to a source, evidence, or other non-compiled artifact, however large or heterogeneous
- **THEN** no structural analysis runs and no suggestion is emitted

#### Scenario: A deliberate hub is not told to split

- **WHEN** a compiled write commits to a page that declares itself a hub by the established convention
- **THEN** no suggestion is emitted

#### Scenario: Advice stops once the material is correctly routed

- **WHEN** the diverged material has been moved into a destination whose declared identity matches it, and further writes are directed there
- **THEN** neither that destination nor the original page emits a suggestion
- **AND** the silence follows from scope agreement rather than from any recorded dismissal

### Requirement: Structural suggestions never weaken a committed write

Structural analysis SHALL be failure-isolated. An exception, an unavailable optional signal, or an undecidable result SHALL cause the suggestion to be omitted, and SHALL NOT fail, delay, retry, roll back, or alter the committed mutation or its terminal.

The system SHALL NOT move, rename, split, retitle, rescope, create, or delete any page, project, or folder as a consequence of structural detection.

Existing write-latency requirements SHALL continue to hold unchanged, including the absolute commit ceilings and the bound on how commit cost may scale with page size.

#### Scenario: A detector failure leaves the write committed

- **WHEN** structural analysis raises during an otherwise successful compiled write
- **THEN** the mutation remains committed with its existing terminal
- **AND** no `structure_suggestion` is present
- **AND** no warning, error code, or retry is produced for the caller

#### Scenario: Detection never restructures the vault

- **WHEN** a suggestion is emitted at any strength
- **THEN** no page, project, or folder has been created, moved, renamed, or deleted by the system

### Requirement: Structural evidence is confined to the written page

Every fact reported in a suggestion SHALL be derived from the page named in the same response. The suggestion SHALL NOT include or allow inference of any other page's path, title, project, tags, or count, whether or not the caller is permitted to see that page.

#### Scenario: The payload discloses nothing about other pages

- **WHEN** a suggestion is emitted for a page in a vault containing pages the caller may not see
- **THEN** the payload contains no path, title, project, or count belonging to any page other than the one written

#### Scenario: The payload is bounded and deterministic

- **WHEN** the same page state is evaluated twice
- **THEN** the reason codes, counts, and recurring terms are identical and identically ordered
- **AND** at most one suggestion is returned, with its recurring-term list bounded to a small fixed maximum
