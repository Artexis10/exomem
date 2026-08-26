## ADDED Requirements

### Requirement: Structured item identity is independent of its human-readable path

The system SHALL keep `collection_id` plus the profile item ID as canonical identity and SHALL treat the Markdown path, displayed title, and managed body as derived representations. Moving or renaming an item SHALL preserve its item ID, collection ID, audit identity, stable reference, and relationship identity.

#### Scenario: Human filename does not replace identity

- **WHEN** a Planning item is created at `Items/Improve onboarding.md`
- **THEN** its canonical identity remains the `plan_id` in frontmatter scoped by the collection rather than the filename

#### Scenario: Rename preserves durable references

- **WHEN** an approved representation migration moves an item to a newly rendered human path
- **THEN** stable references and typed relationships still resolve to the same item identity and audit history

### Requirement: Filename recipes use only stable natural-key fields

An `item_filename` version `1` recipe SHALL declare an ordered non-empty set of schema fields and SHALL be valid only for Markdown-item storage. Planning SHALL permit only its title-bearing natural key. Records SHALL permit only fields declared by the collection's immutable natural key. The renderer SHALL join normalized non-empty values with ` — `, reuse the canonical ordinary-note filename sanitizer and path-byte limits, and add a deterministic short identity suffix when otherwise valid items collide.

Mutable status, lifecycle, priority, commitment, horizon, health, scheduling-window, or other non-identity fields MUST be rejected from the recipe even when their current value is non-empty.

#### Scenario: Mutable workflow state is rejected

- **WHEN** a Planning manifest declares `title`, `priority`, and `horizon` as filename fields
- **THEN** manifest validation refuses the recipe and identifies the forbidden mutable fields

#### Scenario: Immutable event kind may form part of a Record filename

- **WHEN** a Records manifest declares date, title, and event kind as its immutable natural key and filename fields
- **THEN** the recipe is accepted and renders all three values into the human filename

#### Scenario: Collision is deterministic and previewable

- **WHEN** two valid items sanitize to the same filename
- **THEN** each final path is deterministic, at least one carries a short identity suffix, and the exact paths appear in migration preview before writing

### Requirement: New items materialize their readable representation in one write

When a collection declares `item_filename`, creation SHALL use the rendered human path. When it declares `item_presentation`, every successful create, add, append, update, or triage operation SHALL publish the canonical frontmatter and current managed presentation in the same atomic batch. Failure to render either representation SHALL refuse the mutation without writing a partial item.

#### Scenario: New Planning item is readable immediately

- **WHEN** an agent adds a valid item to a Planning collection with both recipes
- **THEN** the committed file has a human title-based filename and a current readable managed block in the same terminal receipt

#### Scenario: Presentation failure cannot leave frontmatter-only state

- **WHEN** a changed canonical value cannot be rendered under the active presentation recipe
- **THEN** the whole mutation refuses and neither canonical nor managed bytes change

### Requirement: Existing filenames move only through explicit representation maintenance

Changing a field used by `item_filename` SHALL NOT silently move an existing item. Inspection SHALL report `filename_drift` with the current and expected governed paths. The drift SHALL clear only after an explicit guarded structured-files apply moves the item or after an explicit manifest revision changes the expected recipe.

#### Scenario: Title edit does not surprise the caller with a move

- **WHEN** a guarded update changes the title-bearing field on an existing item
- **THEN** the item content updates at its current path and inspection reports the expected new path without moving it

#### Scenario: Explicit apply moves the drifted item

- **WHEN** a caller applies the exact current representation plan for that collection
- **THEN** the item moves to the previewed path and inspection no longer reports filename drift

### Requirement: Managed Markdown is meaningful without becoming canonical

`item_presentation` version `1` SHALL render a bounded managed Markdown block containing a human heading, labelled selected canonical values, configured long text, and a Related section. Its marker SHALL bind the recipe digest and canonical item version. Authored Markdown outside the managed block SHALL remain byte-preserved, and direct changes inside the managed block SHALL be reported rather than adopted as canonical state.

#### Scenario: Frontmatter-only Record gains a readable document

- **WHEN** a Records item contains selected summary and note fields with an active presentation recipe
- **THEN** opening the file in an ordinary Markdown reader exposes labelled values and readable note text without requiring frontmatter inspection

#### Scenario: Authored prose survives refresh

- **WHEN** an item has authored Markdown outside its managed markers and a canonical field changes
- **THEN** the renderer replaces only the owned block and preserves the authored bytes exactly

### Requirement: Typed vault relationships render as governed wikilinks

The presentation renderer SHALL resolve and authorize every configured typed vault relationship before rendering it as `[[vault-relative path|human label]]`. Opaque external pointers SHALL remain labelled opaque text and SHALL NOT become vault links. Missing, ambiguous, or withheld targets SHALL follow the existing non-disclosing relationship contract and SHALL NOT reveal a candidate path in rendered or refusal output.

#### Scenario: Planning relationships become graph edges

- **WHEN** a Planning item has authorized area and parent relationships selected by its presentation recipe
- **THEN** the managed Related section contains Obsidian-compatible wikilinks to their current governed paths

#### Scenario: External execution pointer is not a fake vault edge

- **WHEN** a Planning item carries an opaque repository or pull-request execution pointer
- **THEN** presentation labels the pointer without rendering it as a vault wikilink

#### Scenario: Withheld target does not leak

- **WHEN** a configured relationship resolves only to a withheld target
- **THEN** the public result is indistinguishable from the corresponding missing-target case and contains no target path or title

### Requirement: Structured-file migration is exact, preview-first, and atomic

Structured-file maintenance SHALL default to a read-only preview for exactly one selected Planning or Records collection. Preview SHALL return a deterministic plan identity, exact source snapshot, bounded ordered item moves, managed-block writes or removals, governed inbound-link rewrites, collisions, blockers, totals, and truncation state. Apply SHALL require the plan identity and unchanged snapshot, SHALL enter writer authority once, and SHALL either publish the complete previewed transformation or publish nothing.

#### Scenario: UUID collection can be reviewed before migration

- **WHEN** a caller previews a collection whose existing item paths use UUID stems
- **THEN** the result lists every proposed human path and presentation rewrite without changing the vault

#### Scenario: Stale preview refuses

- **WHEN** an item, manifest, or affected inbound link changes after preview
- **THEN** apply refuses as stale and performs no moves or rewrites

#### Scenario: Unowned inbound link blocks a move

- **WHEN** an old item path is linked from append-only, withheld, or otherwise unowned material that cannot be transactionally rewritten
- **THEN** preview reports a blocker and apply refuses rather than stranding the link or creating a redirect note

#### Scenario: Applied migration has an inverse receipt

- **WHEN** a structured-file plan commits successfully
- **THEN** its audit receipt records the exact prior and final paths and content hashes needed for a guarded inverse operation

