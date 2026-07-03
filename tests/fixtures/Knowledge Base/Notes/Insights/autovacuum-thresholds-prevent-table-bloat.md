---
type: insight
status: active
created: 2026-06-03
updated: 2026-06-03
sources:
  - "[[Knowledge Base/Sources/Articles/2026-06-02-postgres-autovacuum-tuning]]"
tags: [postgres, autovacuum, database, bloat]
---

# Per-table autovacuum scale factors prevent bloat on write-heavy tables

## Claim

On a large, write-heavy Postgres table, lower the per-table `autovacuum_vacuum_scale_factor` to 0.01–0.05 instead of leaving the 0.2 default. The default lets a fifth of the table rot into dead tuples before a vacuum fires, which is exactly how big tables accumulate bloat and lose index-scan performance.

## Why it holds

The vacuum trigger scales with table size, so the same 0.2 factor that is fine for a 10k-row table means millions of dead tuples on a 100M-row table. A small per-table override keeps the dead-tuple ceiling proportional to churn, not to table size.

## Where it applies

Any high-churn relational table: event logs, queue tables, session stores.

## Connections

- [[Knowledge Base/Sources/Articles/2026-06-02-postgres-autovacuum-tuning]]
