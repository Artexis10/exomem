# passage-evidence Specification

## Purpose
Let service callers capture their own records as declared passages and rank them as evidence that names the matching record, its position and its hash.

## Requirements

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
The keys SHALL be stored in the page's body, outside the captured text and outside the frontmatter.
The Source's frontmatter SHALL hold the number of declared passages, and its size SHALL NOT grow with that number.
No lexical or vector index SHALL contain a passage key, and no search field SHALL read the keys.
Exomem SHALL store and return a passage key unchanged and SHALL NOT read meaning from it.

#### Scenario: Frontmatter does not grow with the passages

- **WHEN** two Sources are captured with 3 and with 300 declared passages
- **THEN** their frontmatter differs only in the passage count

#### Scenario: Readers of the captured text see passages only

- **WHEN** a reader takes the text that follows the Capture heading of a Source with declared passages
- **THEN** that text holds the passage texts and no key

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
A passage request SHALL require whole-namespace content authority before it reads candidate metadata or encodes the query.
Eligibility SHALL use one proved published corpus generation and maintained eligibility indexes, without a corpus build, offline scan or unrelated pending-page hydration.
The request SHALL read at most `limit` distinct canonical Source pages, selected before byte verification, including selected pages whose chunks are later withheld.
It SHALL NOT read the body or frontmatter of an unselected Source, and SHALL NOT refill the selection after withholding.
A cold or unproved publication SHALL return a visible unavailable outcome before encoding or Source hydration.
Concurrent readers SHALL NOT exclude one another from borrowing a proved immutable generation.
Publisher activity SHALL NOT prevent borrowing while that generation's configuration and full publication checkpoint remain proved.
The outcome SHALL name the existing publication or warm-up action that can make the request ready, and SHALL promise a retry only when publication can progress.
When the cell cannot admit a passage request's model work at once, it SHALL refuse with a busy outcome and a retry hint.
The request SHALL NOT wait for an admission slot. Admitted requests MAY await the existing serialized model execution turn within their bounded admission capacity.

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

#### Scenario: Two passages of one Source both rank

- **WHEN** two declared passages of one Source are the two most similar passages to a query
- **THEN** both appear in the passage results, in score order

#### Scenario: A ranking option is refused with its correction

- **WHEN** a request combines the passage result level with an option that changes ranking
- **THEN** the request is refused before retrieval
- **AND** the error names the option to remove

### Requirement: A passage result identifies the caller's record

Each passage result SHALL carry the Source reference, the Source's `source_identity`, the passage key, the passage ordinal, the SHA-256 hash of the whole passage's text as UTF-8, the span of the matched chunk in the Source body and the similarity score.
The response SHALL also return an opaque `recall_space_id` derived from its reported space identity, matching content-free cell discovery.
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
It SHALL withhold every chunk of a Source whose passage count, number of keys and number of passages in its body are not all equal.
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
Under `service-v1`, passage selection MAY prefer a resource-admitted resident matrix without changing the global service policy or ordinary search selection.
The process SHALL bound numerical allocations across all live passage matrix generations and indexes through an operator-controlled byte budget.
That budget SHALL default to zero, and no tenant request SHALL change it.
Admission SHALL account for replacement scratch before allocation and retain the charge while any reader holds the array.
Concurrent admissions SHALL account for outstanding unmaterialized reservations before spending observed memory headroom.
A denied or cold cache SHALL preserve disk service and report its actual mode; the request SHALL NOT load the full matrix or wait for matrix preparation.
Background preparation SHALL retain existing generation, policy, space and chunk-binding proofs without a full load under the canonical writer lock.
Passage ranking SHALL use disk while a resident generation is known to lack current eligible rows, rather than return a short or altered top-k.
When a stale owner-held cache prevents replacement admission, preparation MAY release that owner's reference and retry boundedly.
Arrays retained by readers SHALL remain charged until release; requests SHALL continue through disk while replacement capacity is unavailable.
Eviction, retirement or space invalidation during preparation SHALL prevent that preparation from publishing a withdrawn cache afterward.

#### Scenario: The bars are measured on the reference

- **WHEN** the latency measurement runs the passage profile on the declared reference
- **THEN** it records the one-caller and four-caller series with their sample counts
- **AND** a series that misses its bar fails the contract check

#### Scenario: Readers retain an old generation during capture

- **WHEN** readers retain an admitted matrix while a capture publishes replacement rows
- **THEN** old and new numerical allocations remain charged to the same process budget
- **AND** changed paths do not escape the existing publication and chunk-binding checks

#### Scenario: The cache cannot fit

- **WHEN** the numerical budget is zero, exhausted or insufficient for preparation
- **THEN** passage search remains available through disk scoring and reports disk mode
- **AND** no unadmitted numerical allocation or full request-time load occurs

### Requirement: A declared passage is embedded exactly as declared

The embedded text of a declared passage SHALL be exactly its declared text.
When a passage splits at the chunk cap, the embedded texts of its parts SHALL join to exactly its declared text.
The cell SHALL NOT add the Source title or any other text to a declared passage.
A Source captured without declared passages SHALL keep its title in its embedded text.

#### Scenario: The title does not change a passage score

- **WHEN** a caller captures the same declared passage under two different titles
- **THEN** the two chunks have equal embedded text and equal vectors
- **AND** a query ranks the two passages with equal scores

#### Scenario: A split passage holds only its own text

- **WHEN** a declared passage exceeds the chunk cap
- **THEN** the embedded texts of its parts join to exactly the declared text

#### Scenario: Other Sources keep their title

- **WHEN** an agent saves a Source without declared passages
- **THEN** the embedded text of its chunks starts with the Source title
