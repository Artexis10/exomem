## 1. Regression coverage

- [ ] 1.1 Prove an unrelated mutation is not blocked while a full rebuild runs.
- [ ] 1.2 Prove a reader during a rebuild sees the old graph or the new one, never a partial one.
- [ ] 1.3 Prove concurrent rebuild requests run exactly one rebuild.
- [ ] 1.4 Prove the existing write-path contract still holds: a write against an
      unusable sidecar indexes the whole vault and leaves it available
      (the contract #346 broke).
- [ ] 1.5 Prove a vault change during the final pass retries rather than publishes.
- [ ] 1.6 Prove exhausted stabilization still marks the graph unavailable.

## 2. Rebuild off the boundary

- [ ] 2.1 Build rebuild passes into a temporary database beside the sidecar.
- [ ] 2.2 Publish by atomic replacement under a boundary hold scoped to the swap.
- [ ] 2.3 Re-verify freshness under the boundary immediately before publishing.
- [ ] 2.4 Verify the replacement behaves correctly on the Windows service path,
      where replacing an open SQLite file does not follow POSIX rename semantics.

## 3. Single-flight

- [ ] 3.1 Serialize rebuilds per vault so concurrent writers do not each start one.
- [ ] 3.2 Resolve waiting requests from the in-flight rebuild's outcome.
- [ ] 3.3 Settle the open question: whether a waiting writer blocks or defers,
      and reconcile that with the contract in 1.4.

## 4. Housekeeping

- [ ] 4.1 Name temporary rebuild databases with a reserved prefix.
- [ ] 4.2 Sweep abandoned temporary databases during reconcile.

## 5. Verification

- [ ] 5.1 Run the epistemic graph, freshness, boundary, semantic unit graph, and
      reconcile suites, plus Ruff and strict OpenSpec validation.
- [ ] 5.2 Re-run the reproduction harness and record the new first-write cost at
      500 and 2,000 pages against the pre-change baseline.
- [ ] 5.3 Confirm under a concurrent-write load that the stabilization budget is
      still sufficient, and raise `REBUILD_STABILIZATION_ATTEMPTS` if not.
- [ ] 5.4 Run the lean suite.

### Baseline to beat

Measured on a maintainer workstation against synthetic dense vaults, on main,
one `upsert_after_write` with no usable sidecar:

| vault | first write | steady-state write | ratio |
|---|---|---|---|
| 500 pages | 7,727.8 ms | 314.5 ms | 24.6x |
| 2,000 pages | 32,381.5 ms | 1,186.2 ms | 27.3x |

with `vault mutation boundary held too long ...
operation=epistemic_graph_refresh_paths holder_kind=graph hold_ms=32371.03`.

CI's write-latency benchmark shows `hold_ms=39092` at 2,000 pages and
`hold_ms=172205` at 8,000.

The target is that the *boundary hold* stops scaling with vault size. The first
writer after invalidation still pays the rebuild; what must stop is every other
mutation waiting behind it.

### Prior art

#346 attempted to fix this by removing the escalation so writes deferred to
reconcile. CI rejected it:
`test_refresh_missing_sidecar_routes_to_full_rebuild` requires a write against a
missing sidecar to index the whole vault and leave it available, and three other
tests depend on the same escalation — the spawned-mutator boundary contract,
forced re-resolution after a relation-registry change, and the schema-v3 sidecar
migration. Task 1.4 exists to keep that contract pinned while the rebuild moves.
