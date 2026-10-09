## Why

A Cloud cell re-seeded from an existing vault answered dense recall from a sidecar that held only two live writes, while the initial build had already embedded about a third of the vault in its own sidecar. Every vector query returned the same two pages, hybrid queries mixed them in, and nothing told the client that the answer was partial.

## What Changes

- While the re-embed job builds a sidecar in the recall encoder's space and the active pointer does not name it, the vector lane reads that sidecar. The serving sidecar joins only when it records exactly the same space, so that live writes count. This applies to an initial build and to a cell whose serving sidecar is refused.
- A response read from a partial build adds the existing `embeddings` component to `warming.components`. It carries no other new field; the build's progress stays in the runtime status and doctor.
- The build runs as bulk model work, and a resident encoder is returned without a model-slot turn, so a query's encode waits for at most the one build passage in flight.
- Doctor reports an initial build as in progress after a live write gave the serving sidecar the build's space, and no longer reports a cell's refused sidecar as dense recall off while its build serves.
- The `cloud` image sets `EXOMEM_DISABLE_CLIP=1`, because it carries no CLIP stack, and a media job queued before CLIP was turned off skips CLIP as the scan does. A later change brings image search to Cloud.

## Capabilities

### Modified Capabilities

- `multilingual-recall`: dense recall during a build reads the build's sidecar, marks the answer warming, and waits for at most one build passage.

## Impact

- `src/exomem/recall_migration.py`, `find_candidates.py`, `find.py`, `embeddings.py`, `doctor.py`, `media_worker.py` and the `cloud` stage of the `Dockerfile`.
- The find envelope gains no field. `warming.components` can name `embeddings` for the length of a build.
- The encoder keeps one CPU thread on a Cloud cell; `design.md` records why.
