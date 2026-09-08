## Context

See proposal.md. Existing durable_closure_common.py provides exact startup index
membership, hermetic process state, fixture generation, MCP decoding and governed
mutations. durable_closure_benchmark.py provides pinned artifact and extraction
proofs. Its instrumentation and graph availability shortcut are not convergence
oracles. graph_concurrent_convergence.py explicitly drains work and is therefore
unsuitable for observing a running service's natural catch-up.

## Goals / Non-Goals

Measure client-observed contention and graph recovery without altering production
code, invoking maintenance, disabling watchers, or installing timing hooks. This
includes a common Markdown comparison and a separate Exomem media diagnostic.
The broader target is parity or better on verified outcomes, with no consistency
relaxation to obtain faster latency. Live deployment is outside this change.

## Decisions

- Use current remote main revisions fetched at cohort preparation: Exomem
  07ec3232450b5c93a6d0b161aacf1ef9180d4ed4 and Basic Memory
  368e607622e9af3d982d0429cb48cfc6c83521f1. Record the actual Exomem measurement
  commit, both source hashes, locks, Python, dependency inventories and product
  versions. Re-fetch before the final cohort and refresh pins if either advanced.
- Add a current-source comparison adapter rather than changing the historical
  wheel benchmark's version/schema assertions. Run both products serially on
  the same fresh runner with identical generated Markdown and declared corpus
  size, alternating product order across three repetitions.
- The common sequence checks each acknowledged marker replacement with an
  immediate exact-body read and immediate keyword search. Keep refusals, stale
  results and latency, without hiding failures behind retries. Record eventual
  visibility separately if immediate visibility fails. Compare medians and
  observed maxima per repetition; require 100 observations for p95.
  Body comparison removes opening metadata YAML and one terminal LF only:
  Exomem adds a final LF while Basic Memory trims it. Interior bytes, spaces
  and additional blank lines remain significant. This is accepted Markdown
  content equality, not identical serialization of product metadata.
- Exercise graph relation replacement with a known source and two targets:
  establish the old typed edge, replace its target through the public tool,
  then prove the new typed edge is present and the old one absent. Public graph
  results and read-only database evidence remain separate. Polling overhead is
  part of the observed convergence bound.
- Exercise a declared concurrent append burst with distinct tokens. Verify every
  acknowledged token appears exactly once after the burst; retain refusals and
  unknown outcomes explicitly. Finally acknowledge a unique write, terminate
  only the disposable server with SIGKILL, restart against the same disposable
  state, and verify exact accepted content, keyword visibility and graph state.
  This tests process-crash recovery, not power-loss or disk-failure durability.
- Invalid transport/setup/adapter observations cannot pass the collection. An
  observed product failure is valid evidence of a failed product contract. No
  cross-product latency winner is declared for an operation whose correctness
  or recovery proof failed. A broad parity claim requires all declared contracts
  and performance comparisons to pass; unresolved gaps remain explicit work.

- Add scripts/mixed_load_benchmark.py and scripts/mixed_load_graph.py. Reuse the
  existing common Markdown corpus under Knowledge Base/Reference and public setup
  admission. Start each case with complete text membership and a public sentinel
  write followed by the stronger graph proof, outside the measured loop.
- Use one persistent MCP session. A fixed number of sequential foreground cycles
  edits a unique marker in the tracker, reads the exact body, and keyword-searches
  that marker without reranking. Each read/search must prove the latest state;
  normal governed writes retain default durable acknowledgements.
  A returned product refusal is recorded without retrying it; subsequent declared
  cycles continue so recovery remains observable. Transport faults abort that
  loop because completion is unknown. Any refused or incorrect cycle fails the
  overall correctness verdict, even if later cycles and graph catch-up succeed.
- In the media case, concurrently preserve and process repeated groups of the
  existing pinned one-PDF/two-image handles under distinct scopes. Record the
  declared group count, actual paths, bytes/hashes and public completion for every
  group. The matching control runs the identical foreground loop. This measures
  the whole ingest burst, with submission calls identified separately. It does not
  isolate extraction CPU from durable sidecar/indexing work or network fetches.
- Record monotonic client intervals at completion, including failures. Poll media
  at a fixed modest interval; retain poll timings separately from foreground
  latency. Report pending-window overlap as an observed upper bound, not an exact
  worker execution interval. p95 requires at least 100 samples in the cohort.
- Inspect graph only during setup and after foreground/media finish. Require
  nonlegacy coherent lineage, matching acknowledgement, empty graph recovery
  receipts, exact file source hashes/membership in a read-only SQLite transaction,
  and stable lineage/file guards around the inspection. Check the fixture tracker
  graph node and expected link; report full/semantic queue counts independently.
  Never run an external drain or use a second full rebuild in the live test vault.
- Run smoke cases first, then at least three fresh idle/media pairs at a declared
  corpus size, alternating case order and quiescing task-owned tests. Keep raw
  samples and runtime/driver/source/fixture hashes. Report host load and dependency
  versions including the Tesseract binary. A larger corpus is a follow-up only if
  the smaller one leaves the relevant scaling question unresolved.
- Run the measured cohort on one fresh GitHub runner when the local shared host
  is busy. An opt-in workflow invokes a serial cohort runner and uploads raw
  reports, logs and generated media evidence. It runs on opening a
  `perf/mixed-load-*` pull request or explicit manual dispatch, not on every
  documentation push. Fixture state is always new and outside the checkout.

## Risks / Trade-offs

- OCR or network variability → pin bytes, prove outputs, retain individual runs,
  compare paired medians, and label submission and extraction observations.
- Proof scanning costs time → exclude from foreground timing and report catch-up
  as an observed upper bound with poll/proof overhead.
- Finite burst may yield few overlapping writes → disclose overlap sample counts
  and omit unsupported tail percentiles instead of extending the claim.
- Concurrent media publication changes source state → reject unstable snapshots
  and retry within a fixed deadline after writers finish.
- Embeddings are disabled → no claim about GPU contention or embedding catch-up.
