# Governed recall latency

`scripts/recall_latency_gate.py` extends the maintained recall instrument.
The packaged request latency contract owns every ceiling, target, stage budget,
paired margin and sample minimum. This directory owns reference data and schemas.

`--paired` without baseline flags resolves the latest GitHub Release once.
Both persistent workers use the same corpus, cases, encoder digest and two CPUs.
The report keeps the resident profile's artifact digest, which has 16 hexadecimal characters.
The encoder cache uses the separate full hash of the published artifact.
The head arm keeps the parent benchmark interpreter. CI starts it with the frozen `recall-benchmark` dependency group.
The tag arm uses one isolated runtime from its archived `uv.lock`, with only the `embeddings-onnx` extra and no default groups.
The current pinned uv reads that lock with `--frozen --no-config`; it never resolves or updates the tag lock.
[`--frozen`](https://docs.astral.sh/uv/concepts/projects/sync/) reads the existing lock without checking its freshness.
[`--no-config`](https://docs.astral.sh/uv/concepts/configuration-files/) bypasses historical writer settings and persistent configuration.
The runner clears inherited `VIRTUAL_ENV` and places the tag environment inside its owned paired scratch directory.
It checks the lock hash after preparation and verifies the worker's interpreter prefix before accepting its measurements.
Preparation stays outside request timers. Cleanup resumes owned workers before removing the scratch directory and the tag runtime.
Each case runs three times per arm, with the first arm alternating between cases.
The paired run samples only the principals that the contract's `paired.principals`
lists, today the owner. CI resolves the release tag and its peeled commit once.
Every sampler archives that pinned commit. `profile.json` sets four case shards
per series. Each shard prepares the full corpus and warms the full selected
series before sampling its contiguous subset. Original series indices keep
arm order unchanged. Both arms and all repeats for one case stay on one runner.
The unsharded `--paired --series NAME` command retains the original experiment.
The inactive worker stops outside measurement so its native pool cannot slow the other arm.
Each arm has its own index state and writer-lease directory.
A paused worker therefore cannot retain the other arm's policy or mutation locks.
The adapter wraps real timing boundaries and delegates their original bodies.
It does not replace principal resolution or release decisions.
Each worker resolves `ask_memory` from its own command registry and binds it once.
The request timer covers the returned callable, including dispatcher ownership,
terminal checks, postfiltering and logging, for both owner and admitted principals.
The `request-latency-v4` contract names this boundary `recall-bound-command-v1`.
Historical leaf measurements use a different boundary and cannot certify it.
Transport, authentication middleware, FastMCP serialization and the live server's
active-surface descriptor remain outside this in-process measurement.
Live response diagnostics retain their inner boundary and cannot certify these
bound-command bars, even when their corpus and SQLite checks pass.
Owner preparation reconciles the advisory projection and validates its persisted
state before managed startup and sampling. A failed persistence refuses the
benchmark fixture; normal advisory refreshes can still run during measurement.
Explicit fork/join contexts retain each branch's complete lifetime, including gaps and waits.
Anonymous work on different threads has an unknown critical path; shares include only known paths and show their sample count.

Raw producers use `--paired --series NAME --base-tag TAG --base-commit SHA`
with `--shard K --shards 4`. Their `paired_fragment` records claim only
`collected` or `refused`; they never apply an allowance or compute a verdict.
Fragments bind ordered case definitions, profile, prose, contract, source
commits, worker locks, runtime recipes, encoder identity, and the stored corpus.
The corpus digest folds each deterministic path and its raw-byte hash into
the existing page-read loop. It adds no separate corpus scan.

`--aggregate-paired DIR --series NAME --base-tag TAG --base-commit SHA`
with `--producer-outcome RESULT` recomputes
those identities from the current head and verifies the supplied tag's commit.
It selects the latest producer attempt for each shard in the current run,
then requires exactly every case, principal, arm, and repeat. A failed shard's
replacement can join inherited successful shards. A latest refusal stays a
refusal; aggregation never falls back to older samples. Missing, corrupt,
same-attempt duplicates, foreign runs, future attempts, or mixed provenance
produce a visible refusal. All raw versions remain in the artifact, and the
report identifies the selected producers explicitly.
The trusted current sampler matrix result must be successful. A failed or
missing result refuses even when older fragments are complete, including a
retry that crashes before writing raw JSON.
The aggregate retains original fragments and their actual CPU IDs. Comparisons
across runners use stable properties rather than CPU identifiers.

The discovery job checks the instrument once before fan-out. Samplers retain
elapsed workload records. The final `recall-latency-paired-SERIES` artifact
contains the same-named complete JSON and all downloaded raw inputs. A retry
replaces its current verdict while immutable raw attempts remain separate.
The new wall-clock result remains unverified until CI runs this shape.

The decision uses each case's ratio of median whole-call elapsed time.
SciPy supplies the Student-t interval over log ratios.
A lower confidence bound above the contract's margin fails the owner series.
Admitted series and stage ratios remain diagnostic.
Missing observations fail the instrument; absent tag stages remain not comparable.
The comparison applies no absolute ceiling on a shared runner.

`profile.json` fixes corpus generation, SQLite versions, resource settings and
source provenance. `cases.json` fixes source offsets, query lengths and requests.
Queries are explicit requests or literal passages from the public prose.
The temporal series requests recent material.
Each series has 40 distinct queries in the contract's three word bands.
The keyword series includes the short-token query `is`.
Every keyword query has a co-occurrence witness in an emitted paragraph.
The reference uses real query encodes. Only corpus vectors use deterministic
normalized vectors through the product's index build, with the resident encoder identity.
The worker restores the real encode function before warm-up.
Stored row counts and spot checks establish the reference before measurement.

The reference prose comes from the official CPython 3.13.0 documentation, pinned
to commit `60403a5409ff2c3f3b07dd2ca91a7a3e096839c7`. The snapshot retains the
Python Software Foundation License Version 2 in `LICENSE.cpython.txt`.
`prose-provenance.json` records every source file, its hash, and the snapshot hash.
Docutils 0.21.2 parses 58 source files into prose paragraphs. The extraction
omits tables, diagnostics, unresolved markup, short fragments and paragraphs
with publication privacy findings. The provenance records the exact recipe and omissions.
The resulting 478,350-byte input has 6,250 terms under the generator's
counting rule. These are input measurements; actual generated page, chunk and
unit counts remain unmeasured until the worker builds and verifies them.
Encoder length-band profiling and fresh-connection SQL replay remain unmeasured.
BM25 and keyword rows show their canonical budgets without applying the twice-budget rule.
Encode also remains a diagnostic target. Eligible reference series record an observed encode p95 above its budget as the measured floor.

`--sql-calibration` records lexical rows without loading an encoder. It prepares
the real catalogue, reference index and advisory state before managed startup.
Each reference case runs as hybrid/page and keyword/page for both principals.
The projections preserve query bytes and filters, limit 15 and disabled reranking.
They establish no original-series, encoder or installed acceptance.
The observer retains real connection identities and checks query ownership and closure.
It checks that keyword page resolution and hit construction stay inside the keyword row.
An absent row stays unmeasured. A population band below the contract's sample
minimum stays insufficient; the report also combines each band across populations.

The default worker uses the parent interpreter. An external worker requires
both `--sql-worker-python` and its `--sql-worker-prefix`. The launcher must use
the frozen dependencies and expose the same source, vault and state paths.
The source controller records the commit and input digests; the worker reports
its actual Python, SQLite, compile-options digest and stored rows.
Compare both required SQLite artifacts by case, principal and projection.
Check common input and corpus digests, then BM25, keyword and final-hit digests.
Resolve a mismatch against generated source truth. Host and container timing
differences do not establish a product gain. Both rows remain diagnostic targets.

`--reference` records an absolute workstation run. Unsupported SQLite versions
receive no ceiling verdict. `--served-url` with `--live-cases` records a live run.
Live queries stay in a private file, and a disclosed trailing whitespace nonce
avoids the remote result cache. The live run retains its contention count and
p95 before exclusions, and refuses excessive contention. Missing contention
clocks remain unknown. `--corpus-counts` reads actual counts on the served host
and rounds them down to buckets of 500; it never loads a model.

`summary.schema.json` rejects content fields and unknown report fields.
Standard references share one sample and environment definition. Ordinary
Draft 2020-12 validators can validate fragments and aggregate reports directly.
Validate every live summary before publishing it. The summary includes only
case codes, measurements, counts, versions, digests and closed states.
Each worker records `runtime_lock_digest`, the SHA-256 of that arm's source `uv.lock`.
Its closed `runtime_policy` names the launch recipe: `parent-benchmark` or `frozen-tag-runtime`.
The digest identifies the lock input; it is not an inventory of installed packages.
Private startup exchanges verify interpreter prefixes; public reports contain no environment paths.
Runtime preparation and worker failures retain their closed reason and phase. A failed tag runtime produces a refusal without a head runtime fallback.
A failed or refused live result opens follow-up work and gates no release.
Legacy flags retain their narrower observation report and cannot certify this reference.
Failed stages and degraded reads remain unknown samples. A transport failure still produces a validated attempt with a closed error code.

An optional `accept.json` must satisfy `accept.schema.json`.
It names the current tag, covered series, maximum ratios, measuring full-CI run,
reason and reviewing pull request. Each maximum equals that run's upper bound,
rounded up to the next contract rounding step. Independent review checks the run.
A record for an older tag expires automatically and excuses no regression.
The release owner removes the expired record through an ordinary pull request.
