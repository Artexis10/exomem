## ADDED Requirements

### Requirement: Server Recall Never Rebuilds Projection On The Reader Thread

An activated server request SHALL NOT rebuild a recall projection, projected resolver, or catalogue allowlist by walking the vault. It SHALL consume a checkpoint-bound live projection or return an explicit retryable warming/unavailable outcome. Correctness-triggered cache eviction MUST NOT silently convert the next server reader into the rebuild worker.

#### Scenario: Missing projection fails fast

- **WHEN** a server reader requests recall after correctness eviction and no replacement projection is live
- **THEN** it receives the retryable retrieval-warming outcome
- **AND** the full-vault walk and resolver rebuild seams are not invoked on that reader

#### Scenario: Background recovery restores readers

- **WHEN** background seed and catalogue repair publish matching authoritative checkpoints after eviction
- **THEN** subsequent server readers resume using the maintained projection without a process restart

### Requirement: Offline Recall Retains A Correct Fallback

An explicit offline or CLI invocation with no activated runtime SHALL retain a bounded source-of-truth walk fallback so watcher-free maintenance remains correct. The fallback SHALL apply the same access, structured-record, alias, and no-follow policy as maintained publication.

#### Scenario: Offline maintenance can reconstruct recall

- **WHEN** an offline command runs without an event-maintained projection
- **THEN** it may reconstruct the projection from canonical Markdown
- **AND** the result excludes every path the maintained publication policy would exclude
