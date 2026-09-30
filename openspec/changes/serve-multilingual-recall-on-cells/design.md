## Context

`recall_space.configured_recall_model` returned bge-base for any process with a cell flag set, and the hosted image named bge-base explicitly. `recall_migration.run` returned `disabled` on a cell. `EmbeddingIndex.encoding` refused a sidecar of another model on a cell, because a cell runs no second encoder. The blue/green design (multilingual-recall D9) serves the old sidecar with the old model while the new one builds. That is two encoders resident, which a cell cannot afford.

## Goals / Non-Goals

**Goals:**
- Recall parity between a cell and a personal server holding the same vault.
- One encoder resident on a cell at every point, including during a re-embed.

**Non-Goals:**
- A node-shared encoder process. The measured cost of per-cell bge-m3 over bge-base does not justify a cross-tenant service boundary.
- Moving an existing cell's vectors from a personal server's sidecar. That would carry plaintext chunks through the operator, and it does not apply to other tenants' cells.
- Memory bounding generally, which `bound-cell-memory` owns.

## Decisions

### D1. Cells default to bge-m3

`configured_recall_model` reads only `EXOMEM_RECALL_MODEL` and otherwise returns bge-m3 everywhere. The hosted build stage and runtime set `EXOMEM_RECALL_MODEL=BAAI/bge-m3`, so the build fetches exactly what cells load. The build's offline gate asserts the declared width (`recall_space.declared_dim`), not the legacy 768.

### D2. A cell re-embeds with one encoder

The job runs on a cell. `preload_serving_encoder` already does nothing on a cell, so the old model is never loaded. `EmbeddingIndex.encoding` keeps refusing the old sidecar on a cell. Queries get `vector_space_mismatch`, and a write's encode soft-fails (`embedding_encode_failed`) and leaves the page for the build's mtime-driven catch-up. The build, cutover and next-start retirement are the personal server's, unchanged. The alternative, serving the old sidecar with a second resident model, costs ~680 MiB for the length of the build on every migrating cell.

## Risks / Trade-offs

- **Dense recall is off during an existing cell's re-embed.** Lexical, keyword, graph and temporal lanes serve. Doctor and `exomem status` report progress and ETA. The only existing cell is the owner's.
- **The image grows** by the difference between the two artefacts (~0.55 GB int8 against ~0.44 GB fp32), and the release build fetches a release asset.
- **Rolling back** to an image on bge-base after the cutover leaves that cell with a bge-m3 sidecar it refuses, and older images neither carry the bge-m3 artefact nor re-embed on a cell. Dense recall is then off until the next roll forward.
