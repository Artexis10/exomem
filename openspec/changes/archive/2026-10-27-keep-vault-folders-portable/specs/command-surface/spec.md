## MODIFIED Requirements

### Requirement: Append-Only Tree Relocation

A move that stays within one append-only tree SHALL be permitted, carrying bytes verbatim. A move from `Sources/` into `Evidence/` SHALL be permitted as a promotion when the caller supplies a promotion reason, and the reason MUST be recorded in the activity log. Every other boundary crossing MUST be refused, including any move out of `Evidence/` and any move into an append-only tree from a non-append-only location. Promotion MUST NOT rewrite file content.

A move that updates wikilinks SHALL be refused with `APPEND_ONLY` when it would rewrite a link inside a page in `Sources/` or `Evidence/`. A page withheld from a restricted mover stays unchanged instead, and the move commits. A generated `index.md` in those trees is not captured content, and the move SHALL rewrite it. When every link that the move rewrites in an `Evidence/` page sits in the page's `ingested_into` field, the move SHALL rewrite that field alone. The body and every other field of that page SHALL keep their bytes.

#### Scenario: Source becomes case-relevant

- **WHEN** a caller moves a page from `Sources/` to `Evidence/<scope>/` with a promotion reason
- **THEN** the file is relocated with its bytes unchanged
- **AND** the activity log records the promotion together with the supplied reason

#### Scenario: Promotion without a stated reason

- **WHEN** a caller moves a page from `Sources/` to `Evidence/<scope>/` without a promotion reason
- **THEN** the move is refused
- **AND** the refusal names the missing reason rather than the append-only rule

#### Scenario: Evidence is never demoted

- **WHEN** a caller moves a page out of `Evidence/` to any destination
- **THEN** the move is refused regardless of any reason supplied
- **AND** the refusal states that a case scope must remain complete

#### Scenario: Outside content still lands through the capture writers

- **WHEN** a caller moves a page from a non-append-only location into `Sources/` or `Evidence/`
- **THEN** the move is refused
- **AND** the refusal directs the caller to `add` or `preserve`

#### Scenario: Evidence names the moved note as ingested

- **WHEN** a caller moves a note with `update_wikilinks=true`, and an `Evidence/` page names that note only in its `ingested_into` field
- **THEN** the move commits and the `ingested_into` entry names the new path
- **AND** the page's body and other fields keep their bytes

#### Scenario: A link in Evidence content still blocks the move

- **WHEN** an `Evidence/` page also links the moved note from its body
- **THEN** the move is refused with `APPEND_ONLY`
- **AND** no page is changed
