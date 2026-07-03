---
type: source
source_type: article
captured: 2026-06-02
url: https://www.postgresql.org/docs/current/routine-vacuuming.html
tags: [postgres, autovacuum, database, bloat]
ingested_into: []
---

# Source: Tuning PostgreSQL Autovacuum to Control Table Bloat

> Captured while diagnosing slow queries on a write-heavy Postgres table — the raw reference on autovacuum knobs.

## Capture

PostgreSQL reclaims dead tuples with autovacuum. The trigger is governed by `autovacuum_vacuum_scale_factor` (default 0.2) plus `autovacuum_vacuum_threshold` (default 50): a vacuum fires when dead tuples exceed `threshold + scale_factor * reltuples`. On a large, write-heavy table the default 0.2 scale factor means one fifth of the table must turn to dead tuples before a vacuum runs, so bloat accumulates and index scans slow down. Lowering the per-table scale factor to 0.01–0.05 and raising `autovacuum_vacuum_cost_limit` makes vacuum run more often and finish faster.

## Why captured

Primary reference behind the compiled insight on autovacuum thresholds; keep the raw knob defaults here.
