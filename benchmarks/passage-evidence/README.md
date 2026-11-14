# Passage latency qualification

`inventory-profile.json` declares the separate Source inventory reference.
`inventory-prepare` generates 20,000 canonical Sources through the existing renderer and capture digest generator.
Each Source declares 27 passages of 40 public-prose words, producing 540,000 chunk rows.
The corpus-only vector generator remains deterministic and normalized; it establishes no retrieval quality.
Preparation reads the existing pinned served artifact and its profile without loading an encoder or acquiring model files.

Preparation freezes generated identities, Source hashes, capture digests, references, recall space, cases and attempt slots.
Generated Source bytes supply the expected values; inventory responses never supply their own answer key.
The five-part inventory key determines lexical ordering and the documented scope-bound cursor.
The plan fixes 100 keyset pages at limit 200 and the same afterpoints at limit two.
Three serial repeats schedule 300 attempts per condition and 600 attempts overall.
Every expected item must match its generated identity, hash, reference, capture digest, recall space and exact 27 indexed chunks.
The plan also requires complete publication, no identity conflicts and the exact next cursor.

The existing MCP worker socket boundary and `exomem.recall_canary.measure_call` remain the measurement instrument.
Both limits have a strict p95 bar below 200 ms under two CPUs, 3 GiB, no swap and a zero-byte passage matrix budget.
The route has no previous latency baseline; zero and speedup claims would misrepresent unavailable evidence.
The inventory reference covers file mode. Store HTTP startup remains unsupported and unqualified.

[The 14 November inventory reference](inventory-qualification-2026-11-14.json) records the independently audited frozen candidate.
All 600 timed calls completed, with 9 SQL statements and no observed request Source reads or encoding.
For limits two and 200, p95 was 14.244 ms and 53.859 ms; one call at limit two reached 475.992 ms.
The run reached its 3 GiB memory cap without OOM. It establishes no spare capacity or production qualification.
The result names later-change limits and retains cold readiness separately from timed calls.

Future worker readiness must use ordinary public inventory calls and the normal watcher and proof drain.
Warm-up traverses every limit-200 page, waiting for exact indexed items before advancing.
Retain the successful traversal receipt with 20,000 distinct generated identities and all warm-up responses separately.
The bounded 3,600-second readiness window allocates 1,000 seconds for the drain's minimum wake cadence,
2,000 seconds for canonical reads and exact SQL checks, and 600 seconds for publication and startup.
After that traversal, the unchanged 180-second quiet gate must pass before timing begins.
The report retains client readiness, full inventory readiness and total time until timing can begin.
It does not require global background quiet before the route can establish its declared readiness.
Readiness never injects proof into a timed request.
All scheduled attempts count, including pending, failed, missing and unattempted calls; no call is retried or excluded.
Every timed call must finish and satisfy its expected response before qualification.

`inventory-prepare` does not start the public worker or run that latency population.
Its offline SQL receipt compares statement counts at limits two and 200 on the generated chunk table and retains the indexed query plan.
That receipt does not establish public-request SQL counts, Source body reads, encoder calls or worker latency.
Those observations need request context, separate from legitimate background warm-up proof reads.
The final worker run must retain native launch identity, source and dependency manifests, terminal journals and resource observations.
Preparation records raw cgroup memory, CPU and I/O observations in `resources.jsonl`.
An accepted frozen source and a matching runtime remain prerequisites for the final worker qualification.

`inventory-measure` consumes the retained fixture through `--baseline` and writes the measured run to a fresh `--root`.
Keep the preparation mounted read-only and separate from the output root.
`--plan-only` validates and freezes the inputs and current source without copying the fixture or starting a worker.
The measurement copies canonical files and state with timestamps intact, then verifies every binding and the ordered chunk-vector digest.
A fresh normal worker publishes and warms its own proofs through ordinary calls; the driver installs no proof receipts.
Warm-up must finish the complete linked traversal before the 600 serial timed calls start.
Each repeat retains its own complete limit-200 traversal receipt.

The inventory worker launch hook observes the existing public normalization middleware and producer seams.
A request context crosses the synchronous SDK boundary; the independent proof-drain thread has no such context.
The hook counts SQLite statements without storing SQL and correlates each row with the native ledger request ID.
It records canonical reader calls, Source file read opens, descriptor read operations and bytes, and `embed_texts` calls separately.
Canonical reader calls count producer invocations, including nested invocations; they are not a physical-read total.
The file-open count is a conservative bound for read attempts; the descriptor counters also cover guarded directory-relative reads.
Qualification requires zero Source reader, file-open, descriptor-read and encoder work within every timed request context.
This prevents an unobserved Source read from certifying the route; a metadata-only Source open also prevents qualification and needs inspection.
Background proof reads remain outside those counters and remain visible in whole-run resource observations.
SQL counts must match between both limits at every shared cursor position and repeat.

The driver flushes each terminal before reading native logs or count observations.
Missing, malformed or mismatched observation rows prevent qualification and preserve the known terminal and duration.
An interrupted run retains every remaining slot as unattempted, with unknown timing.
The report also checks unchanged candidate source, retained input metadata, canonical Source hashes and ordered chunk-vector rows.
Resource snapshots before and after timing separate the timed interval from fixture copying, startup, warm-up and final verification.

`profile.json` fixes the passage reference and the paired disk/resident protocol.
Both modes run the same candidate source, including the publication repair.
The original failed disk qualification remains historical evidence.
It cannot establish the resident cache's gain because it used different publication behavior.

The reference contains 1,500 canonical Sources and 27 declared passages per Source.
Each passage contains 40 words from the pinned public prose in `../recall-latency`.
The original generator uses `add._render_source`, deterministic identities, and canonical capture digests.
Only corpus vectors use the deterministic normalized method from `recall_latency_gate._prepare_worker`.
Every measured query uses the real, warm encoder from the served artifact.
The run makes no claim about recall quality.

The driver reuses `mcp_read_latency_gate.Arm` for the worker, coordinator, isolation, and lifecycle.
It uses Cloud bearer authentication and the normal file watcher.
The watcher supplies the live publication caption that passage reads require.
The driver removes the helper's preload override because `service-v1` rejects it.
`exomem.recall_canary.measure_call` remains the sole call timer.

The `paired` operation takes a retained baseline through `--baseline` and a fresh output root through `--root`.
Mount the retained baseline read-only and keep its path separate from the output root.
The output root must preserve the original internal path `/proof/full/arm/vault`.
The driver copies the canonical vault and state with timestamps intact.
It verifies all declarations, capture receipts, stored chunks, normalized vectors, and lexical hashes before each segment.
A hash over the ordered vector rows must match the retained baseline before and after each segment.
The paired operation does not regenerate passages or re-embed the reference.

`paired --plan-only` writes the protocol, attempt slots, exact source bytes, and query bytes without starting a worker.
The `prepare` and `measure` operations remain available for the original single-mode workflow.
The `--smoke` option applies only to that workflow and cannot report a qualifying pass.

Use an isolated container runtime with these properties:

- Enforce two CPUs, a two-CPU affinity mask, 3 GiB of memory, and no swap through cgroups.
- Keep the Cloud resource policy at `service-v1`, CPU threads at two, and synchronous workers at eight.
- Set `EXOMEM_PASSAGE_MATRIX_BYTES` from the profile: zero for disk and 512 MiB for resident.
- Disable external networking and published ports; keep the coordinator and worker inside the container.
- Mount the candidate source read-only and use an isolated interpreter that imports that source.
- Match installed dependencies to the candidate lock and retain the served encoder artifact identity.
- Mount only the synthetic fixture and required runtime artifacts, with no host credentials or private corpus.

The driver checks the resource limits, source origin, and dependency versions.
A small prefix to the existing worker launch guard writes the actual worker's PID, interpreter, module origins, and dependency identity.
The controller verifies that receipt against its worker PID and frozen source manifest.
This adds no product endpoint or request timing layer.
Existing worker logs retain native encoder settings and real encode spans.

The paired operation preserves the original 40 query IDs, query strings, and arguments.
It freezes 1,200 attempt slots before sampling: 600 per mode.
Three repeats produce 120 single-client calls and 480 four-client calls for each mode.
Mode order alternates by repeat: disk/resident, resident/disk, then disk/resident.
Only one worker and encoder run at a time.
Each segment starts four independent MCP clients and SDK sessions.
The normal stateless HTTP server does not allocate remote MCP session IDs.

Warm-up can trigger the product's normal background cache load.
The driver polls within the profile's bounded warm-up window and retains every warm-up response separately.
Before timing, every client must receive 30 hits, zero withheld chunks, the recorded recall space, and the requested actual scoring mode.
The driver neither loads the full matrix on a request nor forces the cache state.
A mode that does not become ready prevents qualification; remaining scheduled slots stay explicitly unattempted.

An asynchronous barrier releases each four-client batch.
Native worker start and finish records report the observed overlap, including partial overlap.
Client scheduling alone cannot establish four-request worker overlap.
Every measured terminal remains in the absolute latency population, including failed and refused calls.
Every missing slot remains visible with an unknown timing.
No measured call is retried, and no pressure observation excludes a sample.

The existing latency contract and watcher judge both strict p95 bars.
Single-client p95 must be below 200 ms; four-client p95 must be below 500 ms.
All 600 calls in each mode must succeed with 30 hits, zero withheld chunks, and the required actual mode.
`MODEL_BUSY` has no allowance.
Missing calls, failed responses, partial four-request overlap, changed vectors, or changed source prevent that mode from qualifying.
Choose disk if it qualifies; otherwise choose resident if it qualifies.
If neither mode qualifies, the result is unqualified.

The comparison reports resident-minus-disk mean latency for each query and caller condition.
Each query clusters all repeats and concurrent-client slots before the interval calculation.
The maintained recall-parity bootstrap resamples those query differences 10,000 times with seed 73021 and 95% confidence.
The interval is conditional on the captured rounds; these are not independent host experiments.
An interval containing zero means no change.
Failed cases leave the comparison unqualified; incomplete cases prevent an interval.
Fast refusals from a failed mode cannot establish a speedup.

`resources.jsonl` records cgroup memory use, peak, events, and working-set counters throughout each segment.
It also records CPU use, throttling, cgroup pressure, and host CPU/memory pressure.
Cgroup peak memory is cumulative across the container.
The report labels each segment's sampled memory maximum and counter deltas separately.
Missing resource reads remain unavailable, and host pressure supplies context only.

The top-level `samples.jsonl` retains every scheduled slot and response.
Each segment flushes completed responses and timings to `terminals.jsonl` before it reads native dispatch logs.
A failed dispatch observation stays explicit in `samples.jsonl` and does not discard those terminals.
Paired aggregation reads terminal journals first and overlays dispatch observations by unique attempt slot.
Missing or unreadable enrichment preserves the known response and duration and prevents qualification.
Each segment retains warm-up responses, native worker logs, the call ledger, launch identity, resource observations, and fixture receipts.
`frozen-source` preserves source bytes, while before/after manifests detect changes during sampling.
The current reference covers file mode, the declared synthetic load, and the MCP worker socket boundary.
It does not qualify store startup, production load, or retrieval quality.

The [2026-11-14 qualification](qualification-2026-11-14.json) records the frozen source, artifact checksums, runtime conditions and resource observations.
The [case differences](paired-case-differences-2026-11-14.csv) retain all 40 query clusters for each caller condition.

| Mode | Single-caller p95 | Four-caller p95 |
| --- | ---: | ---: |
| Disk | 282.329 ms | 1,511.468 ms |
| Resident, 512 MiB matrix ceiling | 156.947 ms | 431.851 ms |

All 1,200 measured calls succeeded with 30 hits, zero withheld chunks and the requested scoring mode.
The disk-first rule selects resident for this reference because only resident meets both bars.
Its mean differences are −137.529 ms and −790.687 ms for one and four callers.
The corresponding 95% intervals are [−144.224, −132.668] ms and [−829.041, −752.318] ms, conditional on the captured rounds.

The container peak reached 3 GiB, including substantial file cache.
The final segment recorded 2,352 memory-max events and zero OOM events or kills.
Resource observations include startup and warm-up; the peak remains cumulative across segments.
The qualification covers source `0e5a3bc84`, the file-mode reference and the real served query encoder with synthetic corpus vectors.
