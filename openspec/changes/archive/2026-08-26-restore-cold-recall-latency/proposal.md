## Why

Production recall can spend tens of seconds walking and canonicalising the full vault on a reader thread when the event-maintained recall projection is not yet authoritative or has been invalidated. This regresses earlier checkpointed-projection and cold-resolver work, makes connector requests exceed their transport budget, and lets readiness report healthy while ordinary recall is still unusable.

## What Changes

- Make an accepted server recall request consume a checkpoint-bound maintained projection or an explicitly degraded lexical projection; it must not reconstruct ordinary-recall admission by walking the full vault on the request thread.
- Treat recall-projection availability as part of startup and runtime readiness. The service may bind early, but it must distinguish fast partial recall from full hybrid readiness instead of failing ordinary reads with `RETRIEVAL_INDEX_WARMING`.
- Publish watcher seed/reconcile results atomically with the recall checkpoint and preserve the last proven projection until its replacement is authoritative.
- Keep Windows path-alias and no-follow validation at event/publication boundaries, where changed paths are validated once, rather than repeating filesystem canonicalisation for every candidate on every query.
- Order startup as lexical catalogue and recall projection, retrieval models, durable semantic backlog drain, then optional cache warming. Graph, referent, vector, reranker, CLIP, and other heavy enrichment remain optional and soft-failing; unavailable or warming lanes cannot block lexical recall.
- Attribute projection acquisition, watcher seeding, resolver acquisition, and referent enrichment in request timings so large read-path costs cannot disappear into `unattributed_ms`.
- Add small deterministic regression gates for request-path complexity and degraded-read semantics. Production-sized Windows latency, restart, and recovery measurements remain release/nightly evidence rather than expanding ordinary pull-request CI in this runtime change.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `find-recall-efficiency`: Require server reads to avoid full-vault admission walks, expose all material read-path timing, and bound cold and steady-state recall latency without changing ranking semantics.
- `instant-start`: Define lexical-first partial recall and full hybrid readiness around an authoritative recall projection, with heavy model lanes remaining optional and soft-failing. Embedding and reranker models remain deterministic measurement components under the pure-substrate boundary; no reasoning model is introduced.
- `install-readiness`: Make health and readiness surfaces distinguish transport availability, fast partial recall, and full hybrid acceptance.
- `live-index-freshness`: Publish seeded and reconciled recall projections atomically, preserve the last proven checkpoint during replacement, and keep Windows alias validation on bounded event/publication work.
- `recall-read-path`: Prohibit service request threads from rebuilding the recall projection or resolver through a full vault walk while retaining a correct explicit offline/CLI fallback.

## Impact

The primary code impact is in recall freshness/projection ownership, watcher startup and reconcile publication, request-path projection and resolver acquisition, readiness reporting, and timing attribution. Existing lexical, embedding, graph, and reranking sidecars remain derived state; canonical Markdown and structured-record admission rules do not change. The public retrieval result contract gains truthful warming/degraded metadata where necessary, while ranked hits and governed access boundaries remain compatible.
