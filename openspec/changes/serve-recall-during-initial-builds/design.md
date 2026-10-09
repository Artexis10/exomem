## Context

The re-embed job (`recall_migration`) builds a sidecar for the recall encoder's space beside the serving sidecar and publishes it with one pointer swap. Until the swap, recall read only the serving sidecar. After an initial build is interrupted, a live write gives the empty serving sidecar the encoder's identity, so it serves while it holds only that write.

## Decisions

### D1. The job's status names the sidecar that serves during a build

`recall_migration.building_sidecar` reads the in-process job status: the target sidecar, and whether the serving sidecar is served by its own encoder. It costs one `stat` and never walks the vault. The vector lane reads the build's sidecar when the job has a target, the pointer does not name it yet, and the serving sidecar cannot answer with its own encoder: an initial build, or a cell. A personal server migrating from another model keeps the old, complete sidecar and its encoder, as before.

### D2. Two sidecars meet only in one space

The query is encoded for the build's sidecar. The serving sidecar joins the search only when its recorded identity equals that space exactly: model, fingerprint and width. Each page keeps its best chunk score. An answer cached before the build began is not served during it: the build's sidecar name joins the find cache key, and answers marked warming are never cached.

### D3. The existing warming flag is the only disclosure

A partial answer adds `embeddings` to `warming.components`, the component the vector lane already uses while the encoder warms. An agent can tell a partial answer from a whole one; no field or prose is added for a user. Progress (paths done and total, rate, estimate) stays in the runtime status (`recall_reembed`) and doctor. Semantic units are built after each pass's chunks, so unit recall keeps reading the serving sidecar and carries the same flag.

### D4. A query waits for at most one build passage

The build encodes one text per call. It now runs as bulk work, so on the service profile's fair gate a query's encode goes ahead of the build's next passage. `embeddings.get_model` returns a resident encoder without taking the model slot; before, a query waited for a build passage once there and again for its own encode.

Measured on a 600-page vault in a 2 CPU, 3 GiB user scope, from the cell's call ledger, during a 2-thread build: the encode stage's median fell from 198 ms to 148 ms, and the model-slot lookup inside it from 71 ms to 0 (n=10 each). With no build the encode takes 45 ms.

### D5. A Cloud cell's encoder keeps one CPU thread

A second thread makes the build faster and every query that runs during it slower. Queries are constant and builds are rare, so the encoder keeps the one-thread default of the `service-v1` profile. A faster import build comes from reusing stored vectors, in a later change.

Measured on one machine in a 2 CPU, 3 GiB user scope over 600 pages. Build rates come from the build sidecar's chunk count over about 290 s. Query medians come from the cell's call ledger, over 10 hybrid queries each.

| | No build | 1-thread build | 2-thread build |
|---|---|---|---|
| Build rate | | 4.39 chunks/s | 6.66 and 11.82 chunks/s (two runs) |
| cgroup memory peak | | 0.762 GiB | 1.128 GiB at most |
| Hybrid query median | 1,069 ms | 1,157 ms | 2,088 ms |
| Fusion median | 754 ms | 745 ms | 1,437 ms |

Both memory peaks stay under the 2.4 GiB gate (80% of 3 GiB). The query cost of the second thread is CPU contention in fusion.

### D6. Cloud turns CLIP off until it can run it

The `cloud` image has no torch and no Pillow, so every query reported `clip` degraded and the media worker warned once per image. The image sets `EXOMEM_DISABLE_CLIP=1`. The scan already queues no CLIP work then; a durable job queued before the switch now skips CLIP the same way, instead of trying it (and, with a CLIP stack, embedding). A later change replaces this with ONNX image search.
