## ADDED Requirements

### Requirement: Ordinary write bursts retain proportional convergence custody
Related governed writes SHALL preserve exact dirty-path custody and avoid registering independent whole-vault recovery solely because a known intermediate projection has not yet published. Coalescing SHALL prove the union of changed/deleted paths through the latest target. Unknown external edits, incomplete lineage and access changes SHALL retain explicit recovery and freshness fences.

#### Scenario: Unobserved intermediate write generations
- **WHEN** several writes commit while an earlier derived projection is pending and complete intervening path custody is available
- **THEN** derived work converges their latest state without independently rescanning the vault for each intermediate generation

#### Scenario: External content lacks exact lineage
- **WHEN** an external edit cannot be bridged by a complete proven delta
- **THEN** the system preserves the pending/recovery state and does not clear it merely because a boundary wait timed out
