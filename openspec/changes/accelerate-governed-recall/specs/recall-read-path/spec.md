## MODIFIED Requirements

### Requirement: Server Recall Never Rebuilds Projection On The Reader Thread

An activated managed server request SHALL NOT rebuild a recall projection, projected resolver, referent projection, or catalogue allowlist by walking the vault. It SHALL consume one exact checkpoint-bound proof tying maintained catalogues to the live projections used by that request, or return an explicit retryable warming/unavailable outcome. Correctness-triggered cache eviction and downstream resolver or referent acquisition MUST NOT silently convert the next server reader into the rebuild worker.

The same boundary SHALL hold for the lexical corpus and the structured-filter
eligibility catalogue: a server reader SHALL NOT build a lexical corpus or
evaluate a filter by reading pages, and a missing or stale corpus or catalogue
SHALL yield the retryable warming outcome while a single-flight background
repair rebuilds it.

#### Scenario: Missing projection fails fast

- **WHEN** a server reader requests recall after correctness eviction and no replacement projection is live
- **THEN** it receives the retryable retrieval-warming outcome
- **AND** the full-vault walk and resolver rebuild seams are not invoked on that reader

#### Scenario: Background recovery restores readers

- **WHEN** background seed and catalogue repair publish matching authoritative checkpoints after eviction
- **THEN** subsequent server readers resume using the maintained projection without a process restart

#### Scenario: Downstream resolver miss preserves the no-walk boundary

- **WHEN** top-level admission succeeds but a hybrid, vector, resolver, or referent stage cannot obtain the matching maintained generation
- **THEN** that stage declines or returns the retryable warming outcome
- **AND** it does not rebuild from a vault walk on the request thread

#### Scenario: Cold optional referents warm away from the request

- **WHEN** a managed request has an entity cue but no registry cached for its exact live projection
- **THEN** the response omits the optional referent block and schedules one single-flight background build
- **AND** the request thread does not enumerate entity folders or parse the full entity registry

#### Scenario: A lexical corpus miss warms away from the request

- **WHEN** a server reader needs the lexical corpus or the eligibility catalogue and neither is live for the current generation
- **THEN** the reader returns the retryable warming outcome and one background repair is scheduled
- **AND** the request thread does not read pages to build either
