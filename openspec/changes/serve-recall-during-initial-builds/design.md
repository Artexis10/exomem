## Context

The re-embed job (`recall_migration`) builds a sidecar for the recall encoder's space beside the serving sidecar and publishes it with one pointer swap. Until the swap, recall read only the serving sidecar. After an initial build is interrupted, a live write gives the empty serving sidecar the encoder's identity, so it serves while it holds only that write.

## Decisions

### D1. The job's status names the sidecar that serves during a build

`recall_migration.building_sidecar` reads the in-process job status: the target sidecar, and which model wrote the serving sidecar. It costs two `stat` calls and never walks the vault. The vector lane reads the build's sidecar when the job has a target, the pointer does not name it yet, and the recall encoder cannot answer from the serving sidecar: an initial build, a cell, or a personal server whose serving sidecar another build of the same model wrote. That last case was vector-unavailable before; the build is in the resident encoder's own space, so reading it is sound. A personal server migrating from another model keeps the old, complete sidecar and its encoder, as before. A failed build keeps its target, so it keeps serving the pages it built, marked warming, until a restart retries it: that covers more of the vault than the serving sidecar.

### D2. Two sidecars meet only in one space

The query is encoded for the build's sidecar; the vector already encoded for the unit lane is reused when the serving sidecar is in the build's space or empty. The serving sidecar joins the search only when its recorded identity equals that space exactly: model, fingerprint and width.

The build keeps a changed page's old rows until a catch-up pass encodes it again, so a build hit counts only while its rows carry the page's current mtime (`recall_migration.current_in_build`), the rule the build itself uses to decide what it has done. That is one read of the build's sidecar and one `stat` per candidate page. On a cell, a rewritten page then has no dense hit until catch-up, and the lexical lanes cover it. A page current in both sidecars keeps its best chunk score. An answer cached before the build began is not served during it: the build's sidecar name joins the find cache key, and answers marked warming are never cached.

### D3. The existing warming flag is the only disclosure

A partial answer adds `embeddings` to `warming.components`, the component the vector lane already uses while the encoder warms. `warming.since_s` is null there, because the warm-up clock does not time a build; it used to read 0.0. An agent can tell a partial answer from a whole one; no field or prose is added for a user. Progress (paths done and total, rate, estimate) stays in the runtime status (`recall_reembed`) and doctor. Semantic units are built after each pass's chunks, so unit recall keeps reading the serving sidecar and carries the same flag.

### D4. Doctor reads the space comparison and the resume rule from `disk_status`

`disk_status` holds both recorded identities, so it decides whether the build's sidecar is in the serving sidecar's space. For that case it also applies `plan()`'s rule for resuming an interrupted initial build, without loading the encoder. Doctor reports an initial build in progress only when that rule holds. Otherwise it reports the build's sidecar as left over: for example, after an operator reconcile made the serving sidecar cover the vault. Doctor runs in its own process, so it sees a failed build only when it runs in the service's process; the runtime status reports the failure in every case.

### D5. A query waits for at most one build passage

The build encodes one text per call. It now runs as bulk work, so on the service profile's fair gate a query's encode goes ahead of the build's next passage. `embeddings.get_model` returns a resident encoder without taking the model slot; before, a query waited for a build passage once there and again for its own encode.

Measured on a 600-page vault in a 2 CPU, 3 GiB user scope, from the cell's call ledger, during a 2-thread build: the encode stage's median fell from 198 ms to 148 ms, and the model-slot lookup inside it from 71 ms to 0 (n=10 each). With no build the encode takes 45 ms.

### D6. A Cloud cell's encoder keeps one CPU thread

A second thread makes the build faster and every query that runs during it slower. Queries are constant and builds are rare, so the encoder keeps the one-thread default of the `service-v1` profile. A faster import build comes from reusing stored vectors, in a later change.

Measured on one machine in a 2 CPU, 3 GiB user scope over 600 pages. Build rates come from the build sidecar's chunk count over about 290 s. Query medians come from the cell's call ledger, over 10 hybrid queries each.

| | No build | 1-thread build | 2-thread build |
|---|---|---|---|
| Build rate | | 4.39 chunks/s | 6.66 and 11.82 chunks/s (two runs) |
| cgroup memory peak | | 0.762 GiB | 1.128 GiB at most |
| Hybrid query median | 1,069 ms | 1,157 ms | 2,088 ms |
| Fusion median | 754 ms | 745 ms | 1,437 ms |

Both memory peaks stay under the 2.4 GiB gate (80% of 3 GiB). The query cost of the second thread is CPU contention in fusion.

### D7. Cloud turns CLIP off until it can run it

The `cloud` image has no torch and no Pillow, so every query reported `clip` degraded and the media worker warned once per image. The image sets `EXOMEM_DISABLE_CLIP=1`. The scan already queues no CLIP work then; a durable job queued before the switch now skips CLIP the same way, instead of trying it (and, with a CLIP stack, embedding). A later change replaces this with ONNX image search.
