## MODIFIED Requirements

### Requirement: Passage results rank declared passages across Sources

`search_memory` with `result_level: "passage"` SHALL return chunks of declared passages across every eligible Source, ranked by reciprocal rank fusion of their dense similarity to the query and their lexical match with it.
With `mode: "vector"`, the passage level SHALL rank by dense similarity alone, exactly as an unfused request would.
The passage level SHALL accept no `mode`, `mode: "hybrid"` or `mode: "vector"`, and SHALL refuse any other mode before retrieval with an error that names the accepted values.
Lexical matching SHALL use the lexical store's analyser over the stored text of the same chunk rows that dense ranking scores.
The lexical terms SHALL match those rows at the generation that the request reads, or the request SHALL rank by dense similarity alone and say so.
Both rankings SHALL receive the same eligible chunks before ranking, and a lexical candidate SHALL pass the same byte verification as a dense candidate.
More than one passage of one Source MAY appear.
The passage level SHALL honour `limit` up to 200 and SHALL refuse a larger value with an error that names the bound.
The passage path SHALL NOT run graph expansion, page fusion, rerank or usage boosts.
A request that combines the passage level with any other option that changes ranking SHALL be refused before retrieval, with an error that names the option to remove.
A passage request SHALL require whole-namespace content authority before it reads candidate metadata or encodes the query.
Eligibility SHALL use one proved published corpus generation and maintained eligibility indexes, without a corpus build, offline scan or unrelated pending-page hydration.
The request SHALL read at most `limit` distinct canonical Source pages, selected before byte verification, including selected pages whose chunks are later withheld.
It SHALL NOT read the body or frontmatter of an unselected Source, and SHALL NOT refill the selection after withholding.
A cold or unproved publication SHALL return a visible unavailable outcome before encoding or Source hydration.
Concurrent readers SHALL NOT exclude one another from borrowing a proved immutable generation.
Publisher activity SHALL NOT prevent borrowing while that generation's configuration and full publication checkpoint remain proved.
The outcome SHALL name the existing publication or warm-up action that can make the request ready, and SHALL promise a retry only when publication can progress.
When the cell cannot admit a passage request's model work at once, it SHALL refuse with a busy outcome and a retry hint.
The request SHALL NOT wait for an admission slot. Admitted requests MAY await a serialized model execution turn within their bounded admission capacity.
When the served encoder runs inferences concurrently, a passage request's query encode SHALL wait only for other passage requests' query encodes, not for the passage inferences of a capture or a background drain.

#### Scenario: Page reads do not grow with the vault

- **WHEN** a passage request with `limit` 5 runs against a vault with 200 Sources that have declared passages
- **THEN** the public request reads at most 5 distinct canonical Source pages, including terminal filtering
- **AND** it reads no body or frontmatter of an unselected Source

#### Scenario: A restricted caller requests passages

- **WHEN** a caller without whole-namespace content authority requests passage results
- **THEN** the request refuses before query encoding or Source reads
- **AND** the refusal names the authority needed or the ordinary page search alternative

#### Scenario: Publication is unavailable

- **WHEN** the published corpus is absent or its proof is stale
- **THEN** the request returns unavailable without a corpus build, query encoding or Source reads
- **AND** ordinary publication can make the same request ready

#### Scenario: Concurrent readers borrow while a publisher works

- **WHEN** four admitted passage requests borrow a generation whose proof remains current while another publisher works
- **THEN** all four can use that immutable generation without waiting for the publisher
- **AND** no request returns unavailable solely because another reader is proving that generation

#### Scenario: An offline caller has no publisher

- **WHEN** a passage request has no proved publication and no publisher that can make progress
- **THEN** the outcome names the service or warm-up action needed, rather than an unconditional retry

#### Scenario: A selected Source fails byte verification

- **WHEN** a selected Source has stale or malformed passage bytes
- **THEN** its chunks are withheld and counted
- **AND** the request reads no replacement Source beyond its original selection

#### Scenario: The cell is busy

- **WHEN** a passage request arrives while the cell's model admission is full
- **THEN** the request is refused at once with a busy outcome and a retry hint

#### Scenario: A capture encodes its passages

- **WHEN** a capture encodes its Source's passages on a served encoder that runs inferences concurrently
- **AND** a passage request is admitted during that encode
- **THEN** the request encodes its query without waiting for the capture's passage inferences

#### Scenario: Two passages of one Source both rank

- **WHEN** two declared passages of one Source are the two best fused matches to a query
- **THEN** both appear in the passage results, in score order

#### Scenario: A ranking option is refused with its correction

- **WHEN** a request combines the passage result level with an option that changes ranking
- **THEN** the request is refused before retrieval
- **AND** the error names the option to remove

#### Scenario: A passage that dense similarity misses matches the query's words

- **WHEN** dense similarity ranks a passage below `limit`, but the passage's stems match the query's terms
- **THEN** the fused ranking can return that passage within `limit`

#### Scenario: A lexical candidate fails byte verification

- **WHEN** a passage enters the fused top `limit` through stored chunk text that differs from its published Source bytes
- **THEN** its chunk is withheld and counted
- **AND** the request does not refill the selection

#### Scenario: A caller asks for dense ranking

- **WHEN** a passage request sets `mode: "vector"`
- **THEN** its results, scores and backend equal those of dense similarity alone
- **AND** the reply reports `ranking: "dense"`

#### Scenario: Lexical terms are not yet prepared

- **WHEN** the lexical terms do not match the chunk rows at the generation the request reads
- **THEN** the request ranks by dense similarity alone
- **AND** the reply reports `ranking: "dense"` and its summary says that lexical matching is still being prepared

#### Scenario: A writer without lexical terms rewrites a chunk

- **WHEN** a release that does not maintain lexical terms rewrites a chunk row, even under the same rowid
- **THEN** lexical matching resumes only after the terms of every path written since coverage are derived again

### Requirement: A passage result identifies the caller's record

Each passage result SHALL carry the Source reference, the Source's `source_identity`, the passage key, the passage ordinal, the SHA-256 hash of the whole passage's text as UTF-8, the span of the matched chunk in the Source body and a score.
The score of the result at rank `r` SHALL be the dense similarity at dense rank `r` of the same request, and results whose fused values are equal SHALL share the score of the first of them.
Scores SHALL therefore not increase down the results, and the first score SHALL equal the best dense similarity among eligible chunks.
The reply SHALL report `ranking: "fused"` when lexical and dense rankings were fused, and `ranking: "dense"` when dense similarity alone ranked the results.
The response SHALL also return an opaque `recall_space_id` derived from its reported space identity, matching content-free cell discovery.
The reply SHALL name the recall space of the scores.
A passage result SHALL NOT contain passage text.

#### Scenario: The caller maps a result to its record

- **WHEN** a passage result is returned
- **THEN** its key equals the key that the caller declared for that passage
- **AND** its passage hash equals the SHA-256 hash of the text that the caller declared for that passage
- **AND** its span addresses the matched chunk's text in the Source body

#### Scenario: A caller maps fused scores

- **WHEN** a caller maps or floors passage scores with a map fitted to dense similarity
- **THEN** a fused reply's scores have the distribution of a dense-only reply at the same `limit`
- **AND** the reply's `ranking` field tells the caller which ranking ordered the results
