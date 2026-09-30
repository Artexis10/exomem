## Context

`recall_space.configured_recall_model` returned bge-base for any process with a cell flag set, and the hosted image named bge-base explicitly. `recall_migration.run` returned `disabled` on a cell. `EmbeddingIndex.encoding` refused a sidecar of another model on a cell, because a cell runs no second encoder. The blue/green design (multilingual-recall D9) serves the old sidecar with the old model while the new one builds. That is two encoders resident, which a cell cannot afford.

## Goals / Non-Goals

**Goals:**
- Recall parity between a cell and a personal server holding the same vault.
- One encoder resident on a cell at every point, including during a re-embed.
- Hosted cells unchanged.

**Non-Goals:**
- A node-shared encoder process. The measured cost of per-cell bge-m3 over bge-base does not justify a cross-tenant service boundary.
- Moving an existing cell's vectors from a personal server's sidecar. That would carry plaintext chunks through the operator, and it does not apply to other tenants' cells.
- Memory bounding generally, which `bound-cell-memory` owns.

## Decisions

### D1. Cloud cells default to bge-m3; hosted cells keep bge-base

`configured_recall_model` returns `EXOMEM_RECALL_MODEL` when set. Otherwise it returns bge-base on a hosted cell (a malformed hosted flag counts as one) and bge-m3 everywhere else. A hosted cell stays on bge-base because its runtime (`_initialize_locked_hosted_runtime`) starts no re-embed worker, and switching it would leave its vector lane refused with nothing to cut over. Its chart also pins 1536 MiB. No hosted cell runs in production; the cloud cell is the product. Wiring the job into the hosted quiesce lifecycle would be work with nothing to test it against.

The cloud image builds on the hosted image. A `builder-cloud-model` stage fetches bge-m3 into the same model root through the same backend and loads it offline at its declared width (`recall_space.declared_dim`); the cloud stage copies that root and sets `EXOMEM_RECALL_MODEL=BAAI/bge-m3`. The hosted build's own offline gate asserts the declared width too, rather than the legacy 768.

### D2. A cell re-embeds with one encoder

The job runs on a cell. `preload_serving_encoder` already does nothing on a cell, so the old model is never loaded. `EmbeddingIndex.encoding` keeps refusing the old sidecar on a cell. Queries get `vector_space_mismatch`, and a write's encode soft-fails (`embedding_encode_failed`) and leaves the page for the build's mtime-driven catch-up. The build, cutover and next-start retirement are the personal server's, unchanged. The alternative, serving the old sidecar with a second resident model, costs ~680 MiB for the length of the build on every migrating cell. Doctor reports a cell's refused sidecar as dense recall off rather than as serving.

## Risks / Trade-offs

- **Dense recall is off during an existing cell's re-embed.** Lexical, keyword, graph and temporal lanes serve. Doctor and `exomem status` report progress and ETA. The only existing cloud cell is the owner's.
- **Writes during the gap log warnings.** Each write's encode soft-fails and its derived-drain receipt is retried, with a warning each time, until the cutover builds the page. That is noise, not loss.
- **The refused 768-d matrix can stay resident** during the gap wherever warm-up or an audit loads it. It is never resident alongside the new one: the index is replaced at the cutover.
- **The kill switch on a cell** (`EXOMEM_RECALL_REEMBED=off`) keeps dense recall off. The cloud image cannot fall back to bge-base on its own.
- **The cloud image grows** by the bge-m3 artefact (~0.55 GB), and the release build fetches a release asset.
- **Rolling back** to an older cloud image after the cutover leaves the cell with a bge-m3 sidecar it refuses. Older images neither carry the bge-m3 artefact nor re-embed on a cell, so dense recall is off until the next roll forward. Until the first start after the cutover retires it, the bge-base sidecar is still on disk; removing `.embeddings.active` then lets an older image serve it.
