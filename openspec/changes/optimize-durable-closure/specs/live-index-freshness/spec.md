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
