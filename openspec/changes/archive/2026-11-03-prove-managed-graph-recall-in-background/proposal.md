## Why

A managed worker reported `/health/ready` 200 after a promotion, and the first search then took 114.7 seconds. The search computed its find-cache key. That key reads the graph cache token, and the token ran the source-bytes proof of the inherited graph sidecar on the request thread. The proof hashes every page. The spec requires graph recall to prove inline when no other proof is running, so the first request after each promotion, and the first after each write until the graph republishes, paid an O(corpus) proof while readiness said ready.

## What Changes

- In a managed service, a graph recall request that finds no remembered verdict and no running proof starts one background proof of the sidecar. It answers without the graph lane and reports `graph` as degraded. The next request reads the remembered verdict and keeps the lane.
- At most one background proof runs per sidecar. It runs as a bulk pass, so it yields to requests between pages.
- A one-shot process, such as the CLI, keeps the inline proof, because it has no later request to serve.
- A relation filter keeps the blocking proof, because its result depends on the proved sidecar.

## Capabilities

### Modified Capabilities

- `epistemic-graph`: a managed graph recall request hands the cold proof of an inherited sidecar to the background.

## Impact

- Code: `src/exomem/epistemic_graph.py` (`_open_read_snapshot`, new background proof start), comments in `src/exomem/find_candidates.py`.
- Behaviour: in a managed service the first graph recall after a promotion answers within its lexical and dense lanes while the proof runs, and names `graph` in `degraded`. Readiness does not change.
- Undo: revert the change. No stored state changes.
