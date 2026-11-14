## ADDED Requirements

### Requirement: Declared passages become retrieval chunks

A Source captured with declared passages SHALL be indexed with one chunk for each declared passage, in the declared order.
A passage above the chunk cap SHALL split at the cap, and every part SHALL belong to that passage.
The embedding index SHALL keep its stored structure, and a Source captured without declared passages SHALL be chunked and stored as before, with no reindex.

#### Scenario: Each passage is its own chunk

- **WHEN** a caller captures a Source with three declared passages that are each below the chunk cap
- **THEN** the Source has three chunks, one for each passage, in the declared order

#### Scenario: A long passage splits and stays one passage

- **WHEN** a declared passage exceeds the chunk cap
- **THEN** its parts are separate chunks with adjacent spans
- **AND** a passage result for any part carries the key and the ordinal of that passage

#### Scenario: Other Sources are untouched

- **WHEN** a vault holds Sources captured without declared passages
- **THEN** their chunk rows keep their content and order

### Requirement: Passage keys are stored in the Source page and in no index

The passage keys of a Source SHALL be stored in the Source page, in declared order, so that the page's bytes hold the whole declaration in every storage mode.
No lexical or vector index SHALL contain a passage key.
Exomem SHALL store and return a passage key unchanged and SHALL NOT read meaning from it.

#### Scenario: Keys give no search signal

- **WHEN** two Sources are captured with the same title and passage texts and with different passage keys
- **THEN** the indexed text and the vectors of their chunks are equal
- **AND** a query ranks their passages with equal scores

#### Scenario: Both storage modes return the keys

- **WHEN** a Source with declared passages is captured in file mode or in store mode
- **THEN** a passage result in that mode carries the key that the caller declared for that passage

### Requirement: Passage results rank declared passages across Sources

`search_memory` with `result_level: "passage"` SHALL return chunks of declared passages ranked by dense similarity to the query, across every eligible Source.
More than one passage of one Source MAY appear.
The passage level SHALL honour `limit` up to 200 and SHALL refuse a larger value with an error that names the bound.
The passage path SHALL NOT run the lexical lane, graph expansion, page fusion, rerank or usage boosts.
A request that combines the passage level with an option that changes ranking SHALL be refused before retrieval, with an error that names the option to remove.

#### Scenario: Two passages of one Source both rank

- **WHEN** two declared passages of one Source are the two most similar passages to a query
- **THEN** both appear in the passage results, in score order

#### Scenario: A ranking option is refused with its correction

- **WHEN** a request combines the passage result level with an option that changes ranking
- **THEN** the request is refused before retrieval
- **AND** the error names the option to remove

### Requirement: A passage result identifies the caller's record

Each passage result SHALL carry the Source reference, the Source's `source_identity`, the passage key, the passage ordinal, the SHA-256 hash of the whole passage's text as UTF-8, the span of the matched chunk in the Source body and the similarity score.
The reply SHALL name the recall space of the scores.
A passage result SHALL NOT contain passage text.

#### Scenario: The caller maps a result to its record

- **WHEN** a passage result is returned
- **THEN** its key equals the key that the caller declared for that passage
- **AND** its passage hash equals the SHA-256 hash of the text that the caller declared for that passage
- **AND** its span addresses the matched chunk's text in the Source body

### Requirement: A passage result is bound to the current page

The passage path SHALL derive a result's passage and span from the Source's current bytes.
It SHALL withhold a selected chunk whose stored text differs from the text of that chunk derived from the current page.
It SHALL withhold every chunk of a Source whose number of passage keys differs from the number of passages in its body.
The reply SHALL state the number of withheld chunks.

#### Scenario: A stale chunk is withheld

- **WHEN** a selected chunk's stored text does not equal the chunk derived from its Source's current bytes
- **THEN** that chunk is not returned
- **AND** the reply counts one withheld chunk

### Requirement: Eligibility applies before passage ranking

The passage path SHALL resolve filters, governance scope and relation scope before it selects chunks, and SHALL rank only chunks of eligible Sources.
The existing shortcuts for types, tags, source kinds and projects SHALL apply to passage results as they apply to page results.

#### Scenario: A tag filter excludes a better match

- **WHEN** the most similar passage belongs to a Source that the request's tag filter excludes
- **THEN** that passage is absent from the results
- **AND** the results still hold up to `limit` passages from eligible Sources

### Requirement: The passage path has its own latency bars

The request latency contract SHALL declare a reference of at least 1,500 Sources and 40,000 declared-passage chunks on 2 CPUs with a warm served encoder.
On that reference, passage requests at the MCP boundary SHALL meet p95 below 200 ms with one caller.
With four concurrent callers they SHALL meet p95 below 500 ms with no `MODEL_BUSY` refusal.
A reply SHALL report whether a resident matrix or disk scoring served it.

#### Scenario: The bars are measured on the reference

- **WHEN** the latency measurement runs the passage profile on the declared reference
- **THEN** it records the one-caller and four-caller series with their sample counts
- **AND** a series that misses its bar fails the contract check
