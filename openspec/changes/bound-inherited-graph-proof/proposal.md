## Why

A cell restart leaves the epistemic graph sidecar inherited from the previous
process: upgrades, nightly backups and pod restarts all do it. On a ~9,300-file
vault the new process then never went idle. It burned 1.1–1.3 cores for over 50
minutes with no requests, and default `ask_memory` (hybrid, `graph=true`) was
abandoned after 322 s, while keyword recall answered in 2.6 s (#1454).

`_open_read_snapshot` takes its cheap path only at the exact live checkpoint. An
inherited checkpoint never is one: adoption makes it a delta origin, not the
current checkpoint. Every public read therefore re-ran the O(corpus)
source-bytes proof, which re-reads and YAML-parses every page. Its verdict was
never remembered, and four callers repeated it indefinitely:

- the graph drain's availability arm;
- the readiness/coordination probe (`writer_lease` status);
- graph recall, which opened the snapshot three times per request;
- the find result-cache key.

## What Changes

- Remember a public reader's proof verdict per sidecar, keyed by the sidecar's
  stored metadata, its file identity, and the current recall projection
  identity. A change to any of them is a new question and gets one new proof.
  A proof that raised proves nothing and is not remembered.
- Add a no-prove read mode (`availability_state()`, `prove_cold_snapshots=False`)
  that reports `available`, `unavailable` or `unproven` without running the
  proof.
- The readiness/coordination probe reports the remembered verdict, or a new
  `unproven` graph state, and never runs the proof.
- Graph recall is bounded. An unproven sidecar starts one background proof, and
  the recall proceeds without the graph lane, reporting `graph` as degraded.
  The result-cache key uses the same no-prove read. A relation filter still
  requires the proved sidecar.
- The graph drain uses the remembered verdict. A declined proof schedules one
  whole-vault rebuild through the existing marker path and is not re-proved on
  each pass.

No behaviour here runs a model; the pure-substrate constraint is unaffected. The
change is always on and fails closed: an unproven graph is never served as
current.

## Impact

- Code: `epistemic_graph.py` (proof memo, `availability_state`,
  `schedule_availability_proof`, `cache_token(prove=)`), `find_candidates.py`,
  `find.py`, `writer_lease.py`, `runtime_readiness.py`.
- Readiness payload: `coordination.graph_sync.state` may now be `unproven`.
- Tests: `tests/test_graph_inherited_sidecar_proof.py`.
