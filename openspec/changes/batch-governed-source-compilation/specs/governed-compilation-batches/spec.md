# Spec Delta

## Purpose

Reduce transport overhead for grouped Source compilation while retaining independent authority, semantic review and recoverable outcomes for every note.

## ADDED Requirements

### Requirement: Batch input preserves independent note governance

The optional batch form SHALL apply the existing note-creation contract independently to every item. Each item SHALL retain its own draft identity, semantic findings, relation decision, authorized citations and source-version checks. Existing single-note requests SHALL remain supported.

#### Scenario: One draft lacks a relation decision

- **WHEN** one submitted note has a valid review decision and another lacks its required decision
- **THEN** the second note cannot use the first note's decision
- **AND** each item reports its own validation result

#### Scenario: Source authority changes after preview

- **WHEN** a cited Source becomes unauthorized before one item's commit
- **THEN** that item's existing source-closure boundary refuses publication without disclosing withheld material
- **AND** another item's existing authorization does not grant access to that Source

### Requirement: Batch outcomes preserve partial and unknown states

The batch SHALL return one ordered outcome per supplied item. Each outcome SHALL distinguish previewed, committed, refused, unattempted and unknown states using the existing mutation protocol. Committed items SHALL carry their existing receipts. A transport failure SHALL NOT imply that all items failed or that all items committed.

#### Scenario: A later item refuses

- **WHEN** the first item commits and a later item refuses
- **THEN** the response retains the first item's committed receipt and the later refusal
- **AND** the response makes no all-or-nothing claim

#### Scenario: The response is lost after publication

- **WHEN** an item may have committed before transport stops
- **THEN** recovery reconciles its existing draft identity and receipt before repeating the write
- **AND** confirmed committed items are not created again

### Requirement: Source coverage reflects committed live derivatives

Batch transport SHALL preserve the current Source-coverage rule. Previewed, refused, pending, unattempted and unknown notes SHALL NOT count as confirmed live compilation. Each note and its supported Source back-references SHALL retain their existing atomic publication boundary.

#### Scenario: Draft and live notes cite the same Source

- **WHEN** one batch item creates a draft and another commits a live derivative
- **THEN** Source coverage reflects the committed live derivative only
- **AND** each note records only its own declared citations

### Requirement: Agent authorship and existing routes remain available

The agent SHALL choose Source groups and author note content. The batch SHALL NOT invoke a server model to make those decisions. Batch use SHALL be explicit. An unavailable optional batch SHALL leave the existing single-note workflow available and SHALL report unavailable state honestly.

#### Scenario: Batch support is unavailable

- **WHEN** the client cannot use the optional batch form
- **THEN** it can continue through the existing compilation review and single-note writer
- **AND** unavailable batch support does not change note authority or Source coverage
