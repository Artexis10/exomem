# Proposal: bound-graph-recovery-funnel

## Why

The 2026-08 incident's deepest layer was not a code defect but an interaction:
`full_upsert_succeeded` accepts an `epistemic_graph` deferral only while the
registered graph checkpoint equals the live one, so whenever `graph_sync`
sits in `recovery_required`, EVERY batch write fails accounting and mints a
durable full-index receipt for its paths — regardless of the durable
per-path graph receipts the deferral already recorded. That funnel is
correct when recovery completes in minutes. When something makes recovery
impossible — a stale venv `.pth` force-disabling the graph subsystem
(measured: 2,551 receipts, days of churn), or an out-of-process drain
soft-deadlocked against the live service (measured: ~2,143 receipts in
40 minutes) — the funnel self-sustains: backlog → recovery_required →
every write mints → backlog. Draining the accumulated backlog re-embedded
exactly 3 files; the rest was double-accounting. The system had no alarm
for "recovery has been required for hours", no refusal for the concurrent
drain, and only a WARN for the kill-switch that made recovery impossible.

## What Changes

- `full_upsert_succeeded` accepts an `epistemic_graph` deferral during
  `recovery_required` **when and only when** the deferral durably covers the
  batch's graph-input paths with per-path receipts (`graph_upserts`) — the
  receipts ARE the exact durable demand; minting a full receipt on top is
  the same double-accounting shape the warm-up fix (#850) removed for
  embeddings. Uncovered graph deferrals still fail closed.
- Persistent recovery becomes an alarmed, bounded condition: stable
  content-free telemetry for time-in-recovery, surfaced in `/health/ready`,
  and a doctor FAIL (not WARN) when `recovery_required` exceeds a bound or
  when graph work is disabled while a recovery checkpoint exists (the
  provably-unrecoverable combination, including kill-switch env injected by
  unowned site-packages `.pth` files).
- The out-of-process drain (`exomem index`) refuses to start, with a clear
  remediation message, when a live service owns graph work for the same
  vault — the soft-deadlock (CLI holds the graph claim at 0 CPU while the
  service mints receipts) becomes unrepresentable.

## Impact

- `src/exomem/index_sync.py` — the graph clause of `full_upsert_succeeded`;
  deferral telemetry.
- `src/exomem/graph_sync.py` / doctor — recovery-age telemetry, the
  unrecoverable-combination FAIL, `.pth` kill-switch detection.
- `src/exomem/` CLI index path — live-service ownership probe and refusal.
- Regression tests red-first for each: covered-deferral acceptance during
  recovery, funnel alarm, drain refusal.
