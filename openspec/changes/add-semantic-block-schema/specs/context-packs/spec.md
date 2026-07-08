## MODIFIED Requirements

### Requirement: Optional Context Pack Assembly From `find`

The system SHALL provide an optional `pack` parameter on `find` (default
`false`) that, when `false`, returns the existing hit list **unchanged** and,
when `true`, returns an object `{"hits": [...], "pack": {...}}` where `pack` is
an assembled context pack over the top hits carrying `packed_paths`, `claims`,
`semantic_blocks`, `neighborhood`, `contradictions`, `embeddings_available`,
and `truncation`. The assembly SHALL NOT alter the hits, their order, or any
existing `find` behaviour, and the core `find` ranker signature and return type
SHALL be unchanged (the parameter and the object return are confined to the
command leaf).

#### Scenario: Pack off is byte-identical to today

- **WHEN** `find` is called with `pack` omitted or `false`
- **THEN** it returns the same hit list it returns today, with no `pack` object
  and no change to ordering or fields

#### Scenario: Pack on returns hits plus an assembled pack

- **WHEN** `find` is called with `pack=true` over a vault with matching notes
- **THEN** it returns `{"hits", "pack"}` where `hits` is the usual list and
  `pack` carries `packed_paths` (the top notes covered), `claims`,
  `semantic_blocks`, `neighborhood`, `contradictions`, `embeddings_available`,
  and `truncation`
- **AND** no file under the vault is created, modified, moved, or deleted

#### Scenario: Semantic blocks are additive

- **WHEN** a packed page contains supported semantic block headings
- **THEN** `pack.semantic_blocks` includes parsed block dictionaries keyed by
  packed page path
- **AND** pages without semantic blocks do not require any placeholder block
  entries
