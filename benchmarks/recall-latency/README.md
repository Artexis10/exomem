# Governed recall latency

`scripts/recall_latency_gate.py` extends the maintained recall instrument.
The packaged request latency contract owns every ceiling, target, stage budget,
paired margin and sample minimum. This directory owns reference data and schemas.

`--paired` compares the current checkout with the latest GitHub Release tag.
Both persistent workers use the same corpus, cases, encoder digest and two CPUs.
Each case runs three times per arm, with the first arm alternating between cases.
The inactive worker stops outside measurement so its native pool cannot slow the other arm.
The adapter wraps real timing boundaries and delegates their original bodies.
It does not replace principal resolution or release decisions.
Explicit fork/join contexts retain each branch's complete lifetime, including gaps and waits.
Anonymous work on different threads has an unknown critical path; shares include only known paths and show their sample count.

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

`--reference` records an absolute workstation run. Unsupported SQLite versions
receive no ceiling verdict. `--served-url` with `--live-cases` records a live run.
Live queries stay in a private file, and a disclosed trailing whitespace nonce
avoids the remote result cache. The live run retains its contention count and
p95 before exclusions, and refuses excessive contention. Missing contention
clocks remain unknown. `--corpus-counts` reads actual counts on the served host
and rounds them down to buckets of 500; it never loads a model.

`summary.schema.json` rejects content fields and unknown report fields.
Validate every live summary before publishing it. The summary includes only
case codes, measurements, counts, versions, digests and closed states.
A failed or refused live result opens follow-up work and gates no release.
Legacy flags retain their narrower observation report and cannot certify this reference.
Failed stages and degraded reads remain unknown samples. A transport failure still produces a validated attempt with a closed error code.

An optional `accept.json` must satisfy `accept.schema.json`.
It names the current tag, covered series, maximum ratios, measuring full-CI run,
reason and reviewing pull request. Each maximum equals that run's upper bound,
rounded up to the next contract rounding step. Independent review checks the run.
A record for an older tag expires automatically and excuses no regression.
The release owner removes the expired record through an ordinary pull request.
