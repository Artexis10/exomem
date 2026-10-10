## MODIFIED Requirements

### Requirement: Non-Blocking Degradation During Warm

The system SHALL check readiness before a hybrid, rerank, or image-aware `find` lane touches that
lane's model getter, SHALL skip the lane instead of blocking when its component is still warming,
and SHALL still return promptly using the lanes that are ready. The request SHALL NEVER block on a
model-loading lock held by the background warm thread.

A lane whose engine deployment configuration disables, and the image lane while the vault holds
no stored image vectors, SHALL NOT be a component that a request defers. The request SHALL skip
such a lane without touching its model getter, and SHALL NOT report it as warming or degraded.

#### Scenario: Hybrid find degrades to lexical-only results mid-warm

- **WHEN** a hybrid-mode `find` call arrives while the `embeddings` readiness component is not yet
  ready
- **THEN** the vector lane is skipped instead of calling the embedding model getter
- **AND** the call returns promptly using the BM25/keyword/graph lanes that are available
- **AND** the response records `embeddings` as a deferred/degraded component

#### Scenario: Rerank request degrades when the reranker is not ready

- **WHEN** a `find` call with `rerank=true` arrives while the `reranker` readiness component is not
  yet ready
- **THEN** the rerank stage is skipped instead of calling the reranker model getter
- **AND** the call returns the un-reranked ranking promptly

#### Scenario: Image-aware find degrades when CLIP is not ready

- **WHEN** a `find` call that would use the CLIP lane arrives while the `clip` readiness component
  is not yet ready
- **THEN** the CLIP lane is skipped instead of calling the CLIP model getter
- **AND** the call returns promptly using the remaining lanes

#### Scenario: Deferred lane never blocks on the warm thread's model lock

- **WHEN** the background warm thread is inside a model preload for a component
- **THEN** a concurrent `find` request needing that same component does not wait for the preload to
  finish
- **AND** the request instead defers that lane immediately and returns

#### Scenario: Degraded ranking is never stored in the hot find cache

- **WHEN** a `find` call produced mid-warm skipped one or more model lanes — including calls from
  internal callers (link suggestion, evolution, write-time sweeps) that receive no degradation
  signal
- **THEN** that lexical-only result is NOT stored in the hot find-result cache
- **AND** an identical query after the warm completes computes the full ranking instead of serving
  the degraded one

#### Scenario: Write-time corpus sweeps skip instead of blocking mid-warm

- **WHEN** a write (`add`, `note`, `edit`) or a context-pack assembly would run the
  duplicate/contradiction cosine sweep while the `embeddings` component is not yet ready
- **THEN** the sweep returns its documented empty no-op result without touching the embedding model
  getter
- **AND** the write or pack completes promptly without waiting for the warm thread

#### Scenario: A disabled image lane is not degraded

- **WHEN** a `find` call arrives on a deployment whose image search engine is disabled
- **THEN** the CLIP lane is skipped without calling the CLIP model getter
- **AND** the response lists `clip` neither in a `warming` object nor as a degraded component

#### Scenario: A vault without image vectors has nothing to degrade

- **WHEN** image search is enabled but the vault holds no stored image vectors
- **THEN** the CLIP lane is skipped without loading the image query encoder
- **AND** the response lists `clip` neither in a `warming` object nor as a degraded component
