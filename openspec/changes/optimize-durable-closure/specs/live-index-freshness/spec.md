## ADDED Requirements

### Requirement: Ordinary write bursts retain proportional convergence custody
Related governed writes SHALL preserve exact dirty-path custody and avoid registering independent whole-vault recovery solely because a known intermediate projection has not yet published. Coalescing SHALL prove the union of changed/deleted paths through the latest target. Unknown external edits, incomplete lineage and access changes SHALL retain explicit recovery and freshness fences.

#### Scenario: Unobserved intermediate write generations
- **WHEN** several writes commit while an earlier derived projection is pending and complete intervening path custody is available
- **THEN** derived work converges their latest state without independently rescanning the vault for each intermediate generation

#### Scenario: External content lacks exact lineage
- **WHEN** an external edit cannot be bridged by a complete proven delta
- **THEN** the system preserves the pending/recovery state and does not clear it merely because a boundary wait timed out

#### Scenario: Snapshot metadata event precedes replacement
- **WHEN** a guarded canonical transaction restores an existing file's timestamps before replacing its bytes
- **THEN** an exact before-image publication token already owns that event, and successful exact after-image publication does not create an external-change epoch

#### Scenario: External restoration follows a successful transaction
- **WHEN** a newly observed edit restores the before-image after a publication token has already succeeded
- **THEN** the event is external, even though those bytes matched the earlier transaction's before-image

#### Scenario: Aborted transaction has an observation still being hashed
- **WHEN** an observed publication intent aborts before watcher hashing has completed
- **THEN** the abort fences and coalesces replay before returning rather than waiting for hashing to install held custody

#### Scenario: Corpus publication could not advance
- **WHEN** exact destination bytes are proved but corpus publication returns failure
- **THEN** the token does not claim success, observed paths retain recovery custody, and downstream fanout retains its corpus-publication fallback

### Requirement: Extracted media is published by the service parent
Disposable extraction workers SHALL hand bounded durable computation results to the service parent. The parent SHALL revalidate the existing source identity and exact sidecar preimage, own canonical sidecar publication, and advance its local retrieval state through the normal governed fanout. Result custody SHALL survive a child or parent interruption without authorizing stale publication or clearing newer derived debt.

#### Scenario: Extraction completes while the child stays alive
- **WHEN** a child durably hands off an extraction result for a claim also containing CLIP or re-embed work
- **THEN** that claim stops before the remaining stages, and the parent publishes the sidecar and requeues only remaining stages under a fresh claim revision

#### Scenario: A refunded attempt number is reused
- **WHEN** a job is deferred and claimed again with the same user-visible attempt count
- **THEN** the older claim cannot publish or finalize a result because its monotonic claim revision differs

#### Scenario: Parent restarts after sidecar replacement
- **WHEN** durable result custody contains an exact prepared target hash and receipt revision, and the current sidecar matches that target
- **THEN** recovery resumes parent publication and graph-epoch repair without repeating extraction or clearing a newer receipt revision

#### Scenario: Failure presentation is pending
- **WHEN** a failed or blocked job still has a durable result awaiting sidecar presentation
- **THEN** joining media work does not report it drained until that result reaches its terminal disposition
