## ADDED Requirements

### Requirement: SQL-Native Lexical Search Backend

The system SHALL be able to serve the bm25 lane from an FTS5 inverted index in a
per-vault lexical sidecar, selected by `EXOMEM_LEXICAL_BACKEND` (`auto` | `fts5` |
`python`, default `auto`), so that per-query lexical cost scales with the query's
matching terms rather than with corpus size. Indexed text MUST be stemmed with the
same Snowball tokenization the in-process scorer uses, applied identically to
queries, so token and stemming semantics are unchanged. Because FTS5's BM25
scoring is a different variant, promotion of this backend MUST be gated by the
golden retrieval floors and their per-query pins (including the stemming pin) —
not by rank-identity. When the backend is unavailable in any way — FTS5 missing
from the SQLite build, the sidecar unreadable, or a runtime error — the lane MUST
soft-fail to the in-process scorer with unchanged results and without recording a
lane degradation. `EXOMEM_LEXICAL_BACKEND=python` MUST force the in-process paths
unconditionally.

#### Scenario: Indexed backend serves the bm25 lane

- **WHEN** the lexical sidecar is healthy and `EXOMEM_LEXICAL_BACKEND` is `auto`
- **THEN** the bm25 lane's ranked paths are produced by the FTS5 index
- **AND** the lane's interface to fusion is unchanged

#### Scenario: Golden floors gate the backend

- **WHEN** the golden retrieval evaluation runs with the FTS5 backend serving the
  bm25 lane
- **THEN** the golden floors and per-query pins hold, including the
  morphological-variant (stemming) pin

#### Scenario: Unavailable backend falls back silently

- **WHEN** FTS5 is unavailable or the lexical sidecar cannot be used
- **THEN** the bm25 lane returns the in-process scorer's results
- **AND** `find` records no lane degradation for the fallback itself

#### Scenario: Kill switch restores in-process behavior

- **WHEN** `EXOMEM_LEXICAL_BACKEND=python` is set
- **THEN** the bm25 and keyword lanes use the in-process paths even where the
  FTS5 backend is available

### Requirement: Keyword Substring Contract Preserved At Scale

The system SHALL preserve the keyword lane's exact matching contract — strict
case-insensitive substring, every whitespace token present, in title or body,
including mid-word matches — when the lane is served by the indexed backend. This
lane's gate is exact parity, not floors: for any query and corpus state, the
indexed keyword lane MUST return the same match set as the reference in-process
substring scan. Needles below the trigram indexable length MUST still honor the
contract via a fallback lookup over the stored raw text.

#### Scenario: Indexed keyword lane matches the reference scan

- **WHEN** the same keyword-mode query runs once under the indexed backend and
  once under the in-process scan, over the same corpus
- **THEN** the match sets are identical, including mid-word substring matches

#### Scenario: Short needles still honor the contract

- **WHEN** a keyword query contains a token shorter than the trigram indexable
  length
- **THEN** the returned match set still equals the reference scan's
