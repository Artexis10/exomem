## ADDED Requirements

### Requirement: Representation migration extends collection audit history

A guarded structured-file apply SHALL extend the selected collection's existing audit chain for every item whose path or bytes change. The same atomic publication SHALL update affected item markers, the manifest head, content-free audit events, managed presentation bytes, governed inbound links, and the inverse receipt. A previously `ok` collection SHALL remain `ok`; a previously `acknowledged_gap` collection SHALL retain the same permanent discontinuity and remain `acknowledged_gap`.

Preview and apply SHALL refuse a selected collection with a pre-existing malformed audit chain. Representation maintenance SHALL NOT rebaseline, hide, delete, or relabel an audit gap.

#### Scenario: UUID rename remains continuously audited

- **WHEN** a healthy UUID-named item is moved and re-rendered by an exact structured-file plan
- **THEN** the item keeps its collection-scoped identity, its final marker and manifest head extend the prior chain, and post-apply inspection reports `ok`

#### Scenario: Migration preserves an acknowledged discontinuity

- **WHEN** a collection at a valid `acknowledged_gap` checkpoint applies an exact representation plan
- **THEN** the new item events extend that checkpoint and inspection preserves the same discontinuity rather than reporting `ok` or a new gap

#### Scenario: Malformed history blocks representation maintenance

- **WHEN** preview or apply observes a fork, missing transition, unmatched marker, ambiguous identity, or other structural audit gap
- **THEN** it refuses without changing any manifest, item, link, activity, or receipt bytes

### Requirement: Structured filenames have one portable physical spelling

Structured filename projection SHALL normalise compatibility-equivalent Unicode to the held-path portable spelling before sanitisation, byte limits, collision detection, preview identity, and publication. Canonical frontmatter values and human headings SHALL preserve their declared Unicode text independently of the filename projection.

#### Scenario: Compatibility character does not diverge at apply

- **WHEN** a natural-key value contains a compatibility character such as the subscript in `CO₂`
- **THEN** preview returns the exact portable path that apply publishes, and a second preview reports no filename move

#### Scenario: Compatibility-equivalent names collide deterministically

- **WHEN** two items would differ only by compatibility-equivalent filename characters
- **THEN** collision handling assigns deterministic identity suffixes before apply rather than allowing the held mutation seam to alias their targets

### Requirement: Planning and Records share checkpoint continuation semantics

Audit reconstruction SHALL recognise both Records `rebaseline` and Planning `plan_rebaseline` as permanent discontinuity checkpoints. A later valid revision or item event SHALL extend either checkpoint while preserving its bounded discontinuity, and SHALL NOT convert a malformed historical chain into an acknowledged gap.

#### Scenario: Planning revision follows rebaseline

- **WHEN** a valid Planning manifest edit is rebaselined with the exact current mismatch codes and then validly revised
- **THEN** both mutations commit, inspection remains `acknowledged_gap`, and the original Planning discontinuity remains visible

#### Scenario: Records revision follows rebaseline

- **WHEN** a valid Records manifest edit is rebaselined with the exact current mismatch codes and then validly revised
- **THEN** both mutations commit, inspection remains `acknowledged_gap`, and the original Records discontinuity remains visible

### Requirement: Failed representation publication restores exact pre-migration state

Structured-file apply SHALL stage all path moves and SHALL publish no visible partial transaction. If any target guard, item/manifest write, audit write, inbound-link rewrite, or receipt write fails, rollback SHALL restore every original path and byte, including compatibility-Unicode moves, and SHALL leave no final target, advanced audit head, or committed receipt.

#### Scenario: Failure after staged compatibility rename is exact

- **WHEN** publication fails after a compatibility-Unicode source has been staged and other targets have been installed
- **THEN** every original path and hash is restored, every planned final target is absent, and collection inspection reports the same status as before apply
