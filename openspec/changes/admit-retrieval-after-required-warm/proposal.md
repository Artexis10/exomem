## Why

At the 0.96.0 promotion of the personal service, retrieval admission was revoked ten seconds into the promoted worker's warm and readiness stayed `not_ready` for 53 seconds. It flipped back at the exact moment the warm logged completion. The warm was held open by the optional reranker preload, which spent 46.7 seconds queued on the model gate. A revoked catalogue is only re-proved once the whole warm has finished, so a model that recall admission does not depend on decided how long the service reported itself unready. The readiness probe also ran its proof on the event loop: one probe blocked for 5.8 seconds on a reserved-state lock and the liveness polls behind it timed out.

## What Changes

- The warm marks its required stages (catalogue, graph handoff, semantic corpus, lexical caches) finished before optional model preloads start. From that point a revoked retrieval catalogue is re-proved exactly as after the whole warm, by the readiness probe and by a refused recall request.
- Optional model preloads continue in the background and no longer hold readiness.
- The readiness probe runs its proof off the event loop, on a small dedicated limiter.

## Capabilities

### Modified Capabilities

- `instant-start`: optional preloads do not delay retrieval readiness; admission is re-proved once the required warm stages finish.

## Impact

- Code: `src/exomem/readiness.py`, `src/exomem/warmup.py`, `src/exomem/find.py`, `src/exomem/server_assets.py`.
- Behaviour: a promoted or restarted worker reports ready as soon as its catalogue proof holds after the required stages, instead of after the last model preload. The readiness payload and its fields are unchanged.
