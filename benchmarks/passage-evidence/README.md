# Passage latency qualification

`profile.json` fixes the disk-only qualification for declared passage evidence.
The decision is whether the existing disk scorer meets both latency bars.
A passing run closes the latency decision without introducing another scorer.
A failing run retains its evidence for the next design decision.

The fixture contains 1,500 canonical Sources and 27 declared passages per Source.
Each passage contains 40 words from the pinned public prose in `../recall-latency`.
The generator uses `add._render_source`, deterministic identities, and canonical capture digests.
Only corpus vectors use the deterministic normalized method from `recall_latency_gate._prepare_worker`.
Every measured query uses the real, warm encoder from the served artifact.
This run makes no claim about recall quality.

The driver reuses `mcp_read_latency_gate.Arm` for the worker, coordinator, isolation, and lifecycle.
It uses the Cloud bearer protocol and enables the normal file watcher.
The watcher supplies the live publication caption that passage reads require.
The driver removes the helper's preload override because `service-v1` rejects it.
`exomem.recall_canary.measure_call` is the sole call timer.

Run preparation and measurement inside one isolated container runtime with these properties:

- Enforce two CPUs, a two-CPU affinity mask, 3 GiB of memory, and no swap through cgroups.
- Set the Cloud resource policy to `service-v1`, CPU threads to two, and synchronous workers to eight.
- Disable external networking and published ports; keep the coordinator and worker inside the container.
- Mount the candidate source read-only and use an isolated interpreter that imports that source.
- Match installed dependency versions to the candidate lock and retain the encoder artifact identity.
- Use only a disposable synthetic fixture, with no host credentials or private corpus mounted.

The driver refuses a runtime that does not meet its checked resource and dependency contract.
Preparation writes `preparation.json` after it checks every Source declaration, capture receipt, stored chunk, and lexical hash.
The checks include normalized vectors, all 40,500 passage bindings, total rows, and three explicit Source examples.
The maintained warm-up must grant retrieval admission before measurement starts.

Measurement freezes 40 reference query IDs, exact query strings, and arguments in `frozen-cases.json` before timing.
The queries come from the reference hybrid series, with the public passage option replacing the reference page option.
Three repeats produce 120 single-client calls and 480 calls from four independent MCP clients.
An asynchronous barrier releases each four-client batch.
Native worker start and finish records must prove that all four requests overlap.
Client-side scheduling alone does not satisfy that check.

Every response must contain 30 hits, zero withheld chunks, the recorded recall space, and `scoring_mode: disk`.
The driver retains every terminal and timing in `samples.jsonl`.
Warm-up responses remain in `warmup.json` and do not enter the qualification population.
A missing, failed, refused, malformed, or empty response prevents qualification.
`MODEL_BUSY` has no allowance.

The existing nearest-rank definition determines p95 over all retained calls in each condition.
Single-client p95 must be strictly below 200 ms; four-client p95 must be strictly below 500 ms.
Source manifests before and after the run must match.
`measure-report.json` records the result, condition counts, limitations, preparation evidence, and runtime identity.
The `--smoke` option uses two Sources and five calls; it cannot report a qualifying pass.

The current reference covers file mode.
It does not qualify store startup, a resident scorer, production load, or retrieval quality.
