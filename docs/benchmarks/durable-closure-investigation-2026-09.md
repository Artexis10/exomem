<!-- authority:non-specification -->

# Durable-closure investigation: September 2026

This investigation measures whether an agent can finish a multi-note evidence
workflow, not just whether a canonical file replacement is fast. The associated
contract is `openspec/changes/optimize-durable-closure/`.

## Current production evidence

The observed service reported 0.71.0. Its installed `epistemic_graph`,
`graph_sync`, `file_watcher`, `index_sync`, `deferred_index` and `derived_receipts`
modules were byte-identical to repository baseline `c4a19779`. Eight prior
diagnostic notes were reconciled against that code and the live logs; historical
measurements are not used as the present baseline.

The selected ledger slice contains the incident client's calls completed between
2026-09-05 11:50:00 and 12:07:00 UTC. Client/time selection is diagnostic, not an
authenticated workflow identity. The current ledger segment's row hashes and
internal sequence chain verified without findings; this was not a genesis-to-live
archive audit. No personal paths, request identities or content are included here.

| Measured quantity | Result |
| --- | ---: |
| Public calls | 35 |
| Sum of server-wrapper execution | 670.566 s |
| Interval-union server occupancy | 665.701 s |
| First inferred call start to last completion | 935.912 s |
| Server-idle time within that observed span | 270.211 s |
| Ordinary recall refusals | 4 |
| Media processing: three calls | 204.004 s |
| Existing-page edits: three calls | 149.642 s |
| Connection discovery: three calls | 99.703 s |
| Direct page reads: ten calls | 92.277 s |
| Observations: three calls | 50.331 s |
| Recall: nine calls | 46.920 s |
| Artifact preservation: one call | 26.561 s |

The approximately fifteen-minute user report is consistent with the ledger
observation span, but the ledger alone does not measure when the user began or
finished. The 270.211 seconds outside server calls cannot be assigned to model
planning, connector transit, user pauses or verification decisions without the
client trace. These fields remain null in the reconstruction.
Interval reconstruction also assumes a continuous UTC clock. The historical
ledger has no paired client monotonic trace to verify that assumption; summed
server durations do not depend on UTC continuity.

Four warming refusals form two sampled refusal-to-next-success spans of 88.893
and 12.486 seconds. They do not prove continuous outages over those intervals.
An additional attempted maintenance call was refused with
`MAINTENANCE_REQUIRES_CLI`.

The mutation journal joins ten calls by request identity. Its recorded boundary
holds range from 8.97 to 89.53 ms; these are not whole-call or complete canonical
transaction clocks. The ledger separately contains `derived.canonical_commit`
spans totaling 5.924 seconds across eleven invocations in seven calls, and
`index.upsert_after_write` spans totaling 114.329 seconds across fourteen
invocations in ten calls. These aggregates have incomplete coverage and may
contain nested work; do not subtract them as an exhaustive decomposition or
derive invocation percentiles from a multi-invocation total.

In the same service-log window, graph dispatch registered whole-vault recovery
24 times: 21 predecessor-unreadable decisions with `external_pending=True` and
three with it false. Three full rebuild publication messages occur in the
window, the earliest before the first selected workflow call. Registrations
can coalesce; neither count is a per-workflow count of independent full scans.
Two watcher-withdrawal attempts exhausted their retries. Graph-drain boundary
holds of 62.065 and 78.616 seconds exceed the watcher's approximately twenty-second
effective withdrawal retry window.

Queues inspected later were empty. That is evidence of eventual convergence,
not evidence that the incident had no deferred backlog. The fast durable-ACK
switch was absent from the installed service configuration and a fresh service
interpreter, consistent with its default-off rollout; this did not inspect the
already-running process's environment.

## Reproduced mechanisms

### A canonical write is mistaken for an external edit

`batch_atomic_write` publishes a destination before `post_commit_batch_fanout`
registers self-write suppression. A watcher event between those operations
misses the registration and immediately marks external freshness pending.

The isolated ordering probe produces:

```text
publish_then_register: external_pending=True epoch=1 watcher_queued=1
register_then_observe: external_pending=False epoch=None watcher_queued=0
```

The pending epoch makes the graph predecessor unreadable. The writer's graph
dispatch registers recovery before entering its exact-path incremental path.
Later withdrawal clears graph metadata, so the next predecessor can still be
unreadable after the external flag clears. This explains a current route to both
observed flag states without reviving the previously fixed transient-lock latch.

Publication intents repair this ordering window only when the exact staged bytes
can be proved. Matching in-flight events are held, not discarded: partial
rollback can leave proposed bytes behind, so failed or expired intents must
fence and replay their observed paths. Foreign edits retain the external-change
path. Fast-receipt behavior remains separately protected.

The combined real-extraction probe exposed two earlier producer gaps that the
post-replacement test did not exercise. Snapshotting an existing destination
reads its bytes and restores timestamps with `os.utime` before the original
intent-registration point. Watchdog reports that metadata operation while the
old bytes remain visible. An after-image-only token cannot prove this event,
even when the subsequent canonical replacement succeeds. A deterministic probe
delivering the event at the timestamp-restore seam still marked external pending
on the first integrated repair.

Media extraction also publishes from a disposable child process. Its deferred
sidecar write does not register a publication intent, and its eventual
process-local self-write registration cannot update the service watcher's
registry. A second interpreter reproduced the mismatch even when both processes
read the identical final SHA-256. Suppressing that event alone would be unsafe:
the parent's freshness, inbound and resolver state must also adopt the exact
publication. These findings require transaction-wide before/after proof and a
publication boundary visible to the parent; they are not evidence that the
historical external-pending latch returned.

### Optional graph expansion vetoes useful direct recall

Managed ordinary retrieval already has maintained catalogue admission and an
exact pending-write overlay. However, its optional graph expansion calls a
strict current resolver. A typed resolver-warming exception escapes candidate
collection and discards otherwise eligible lexical/vector matches.

The regression exercises all four resolver-warming sites in both branches:
unavailable graph fallback and available graph with unindexed legacy seeds.
The repair omits unproven graph contributions and discloses graph warming. It
does not relax catalogue, policy, pending-visibility or relation-predicate proof.

The integrated real-extraction probe also produced `catalog_proof_incomplete`
after repeated lexical source-moved publication aborts and nested-lock deferred
upserts. This refusal is upstream mandatory catalogue admission, not optional
graph expansion. There was no proven coherent catalogue checkpoint to serve;
the repair must remove producer churn or establish an exact usable snapshot,
not broaden the resolver exception handler to swallow this failure.

### Media enqueue pays a multi-consumer convergence bill

`process_media(process|retry)` reconciles an artifact and then calls the general
deferred-index drain. This can execute graph and other queued work before
returning a media result. Removing that drain preserves durable queue custody
under the normal background owner. A bounded selected-path batch removes two
extra public calls for three independent artifacts without creating a
multi-page transaction or concurrent conflicting writers.

### New targets still have a proportionality problem

The appeared-target graph path scans Markdown bodies for previously unresolved
forward links while holding the canonical mutation boundary. An isolated corpus
of 200 pages linking to one newly appeared target scanned and indexed 201 pages
while a competing writer was refused. This is a separate amplifier from watcher
echoes. Production lacked substage timing to assign a percentage of its long
holds to discovery versus reindexing. Workflow instrumentation must retain scan
counts and distinguish this remaining cost from false whole-vault recovery.

## Write-burst and orchestration audit

The current system already has path-level coalescing; the missing property is
that every producer reliably reaches that incremental route.

| Surface | Current mechanism and limit |
| --- | --- |
| Graph/full/semantic queues | One row per relative path with a revision; a completed old snapshot cannot clear a newer enqueue. Graph full-rebuild debt uses an advancing generation marker. |
| Graph dispatch | Exact checkpoint paths and created paths can enter the incremental queue. An unreadable predecessor is tested first and can redirect known writes into full recovery. |
| Fast-ACK component receipts | One successful fan-out is memoized across components of one batch. The memo key includes batch identity and canonical generation; this is not cross-batch coalescing. Failed fan-outs are retried. |
| Resolver, lexical, references, embeddings | The existing fan-out owns these projections together. Embedding jobs have a durable path queue, but deferring them does not by itself avoid graph work in the same fan-out. |
| Watcher | Events coalesce by path after admission. The publication race can nevertheless create an external epoch before that queue helps; exact publication intents address this producer-side amplification. |

Thus N receipts do not necessarily imply N full scans, but N distinct successful
receipt fan-outs remain possible. These repairs do not claim cross-batch receipt
coalescing. Appeared-target discovery and generation-lineage recovery must remain
visible in the workflow measurements rather than being hidden by fast ACKs.

The public surface already supports multiple preservation handles, edits combined
within one page, and observations/relations authored in an initial note. Selected
media paths close one remaining batch gap. Independent reads can share an agent
tool round; conflicting canonical edits still require ordering and hash guards.
The generic dependency structure is preservation → media enqueue and note work →
bounded final verification, with extraction convergence measured separately. No
application-specific command or second multi-page transaction engine is added.

The combined real-extraction stress probe also exposed a necessary dependency:
source closure resolves a binary citation through its evidence sidecar, fingerprints
that source and stages a guarded backlink write. Extraction can change that
sidecar between preparation and commit. The observed `STALE_SEMANTIC_WRITE`
protects fresh extraction from being overwritten; it is not a guard to relax.
Source-independent note work can overlap extraction, but the evidence-backed
write follows the relevant artifact's completion. The current media worker
processes its queue serially; batching removes public round trips and synchronous
graph draining, not that worker's single-owner execution constraint.

## Rerunning the ledger decomposition

Use a selected copy of the service's logs, keeping private inputs outside the
repository:

```sh
python scripts/durable_closure_ledger.py ledger.jsonl \
  --client openai-mcp/1.0.0 \
  --start 2026-09-05T11:50:00+00:00 \
  --end 2026-09-05T12:07:00+00:00
```

The analyzer reports malformed/unreadable input instead of silently dropping it.
It does not authenticate client identity or verify the ledger hash chain; use
the existing ledger verification command for that separate check.

## Resumed baseline and diagnostic measurements

The resumed measurements use product baseline `5fc2e55d` (0.73.1), with the same
product source bytes as integrated main `4f583144`. Each row starts a persistent
MCP service over a fresh synthetic corpus and waits for public semantic-write
validation before starting the workflow clock. Startup is recorded separately.
The PDF and two images come from the pinned
[public artifact manifest](durable-closure-public-artifacts.json); successful
real-extraction rows must prove their three distinct expected texts.

| Source and corpus | Result | Workflow wall | Public calls | Evidence |
| --- | --- | ---: | ---: | --- |
| Baseline, 3,800 pages | Final recall refused; writes and extraction proved | 35.94 s | 61 | [Machine-readable row](durable-closure-2026-09/before-3800-real-optimized.json) |
| Baseline, 8,000 pages | Media reconciliation raced with extraction; dependent write blocked; final recall refused | 60.85 s | 14 | [Machine-readable row](durable-closure-2026-09/before-8000-real-optimized.json) |
| Candidate before parent graph repair, 3,800 pages | Correct final state; parent graph wait made closure unacceptably slow | 214.18 s | 1,244 | [Diagnostic row](durable-closure-2026-09/after-3800-real-optimized.json) |

The baseline rows did not finish successfully, so their elapsed times are not
completed latency baselines and no speed ratio is inferred. Each is one sample
with recorded host load; task test workers were idle, but external host load was
not identical. All three used the same Python 3.13.12 runtime, resolved package
set, and declared optional-model configuration. Real extraction used PyMuPDF and
Tesseract; embeddings and CLIP models were unavailable. The optimized baseline
used three single-path media calls, while the candidate used one selected-path
batch.

The slow candidate completed all direct-read, citation, tracker, stale-relation
and ordinary-recall checks, with no recall refusal. Of its 1,244 calls, 1,227
polled extraction convergence. Its completed image computations waited behind
parent fanout that entered graph rebuilding after releasing mutation authority.
This diagnostic motivates preserving a receipt-backed pending context through
both first publication and exact-target recovery, including inner graph
fallbacks; detaching only the outer dispatcher is insufficient.

Graph timing and invocation wrappers cover the MCP service process only, not its
disposable media children. Source-scan fields cover their named wrapped
functions, not every filesystem read. Full graph/lexical/embedding convergence,
connector overhead and model planning remain null where unmeasured. The
published JSON preserves these limits, per-call observations, corpus digest,
fixture hashes, runtime packages and host metadata; private runtime paths and
the generated per-page inventory are omitted with explicit annotations.

## Final publication and recall repairs

Media children now leave bounded, durable result packets. The service parent
publishes the exact sidecar while checking the claim revision, previous sidecar
and binary identity. A recovered result cannot clear newer receipt debt. Parent
publication keeps its registered graph handoff visible through the exact fanout
acknowledgement, starts that matching handoff, and only then clears the completed
full receipt. A changed checkpoint preserves the newer owner. This closes a
retry loop in which detaching the registration before acknowledgement made the
acknowledgement itself fail.

Ordinary recall also needs one consistent catalogue snapshot for the whole
request. A strict health check or scheduled repair can demote readiness while a
published catalogue remains usable with its exact pending-write overlay. The
request now re-proves that admission and carries the admitted checkpoint into
its lexical reads and indexed-path snapshot. It still rechecks policy, access
and catalogue identity. A cold catalogue, incomplete pending proof, changed
checkpoint or strict relation query continues to refuse unproven results. The
request does not promote global health or waive a mandatory proof.

These fixes preserve useful closure while optional graph work continues. They
do not establish a bound on every background recovery or every possible
interleaving of reads and writes.

## Reviewed workflow measurements

Rows below name their measured source revision and use clean worktrees, Python
3.13.12, a persistent public MCP session and fresh isolated state. Startup
precedes the timed workflow. Each row is one sample with task test workers
idle; the recorded external host load varies. Real extraction uses PyMuPDF
and Tesseract with the three pinned artifacts. Embeddings and CLIP models are
disabled or unavailable. Model-free rows prove preservation and durable
blocked-job custody only.

| Pages | Profile | Variant | Result | Workflow | Startup | Calls | ACK p50 / p95 | Source | Evidence |
| ---: | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| 3,800 | Real extraction | optimized | Pass | 27.98 s | 55.63 s | 20 | 1.89 / 13.51 s | `3d04d8cc` | [JSON](durable-closure-2026-09/reviewed-3800-real-optimized.json) |
| 8,000 | Real extraction | optimized | Pass | 37.11 s | 117.07 s | 23 | 2.68 / 15.37 s | `3d04d8cc` | [JSON](durable-closure-2026-09/reviewed-8000-real-optimized.json) |
| 3,800 | Real extraction | stress | Pass | 32.97 s | 55.19 s | 27 | 2.69 / 13.48 s | `3d04d8cc` | [JSON](durable-closure-2026-09/reviewed-3800-real-stress.json) |
| 8,000 | Real extraction | stress | Pass | 346.55 s | 119.32 s | 27 | 19.92 / 149.59 s | `3d04d8cc` | [JSON](durable-closure-2026-09/reviewed-8000-real-stress.json) |
| 3,800 | Model-free | optimized | Pass | 26.90 s | 58.42 s | 21 | 1.82 / 13.51 s | `fe56a3eb` | [JSON](durable-closure-2026-09/accepted-3800-model-free-optimized.json) |
| 8,000 | Model-free | optimized | Pass | 35.18 s | 127.25 s | 21 | 2.26 / 14.96 s | `fe56a3eb` | [JSON](durable-closure-2026-09/accepted-8000-model-free-optimized.json) |
| 3,800 | Model-free | stress | Pass | 31.97 s | 57.37 s | 28 | 2.04 / 14.49 s | `fe56a3eb` | [JSON](durable-closure-2026-09/accepted-3800-model-free-stress.json) |
| 8,000 | Model-free | stress | Pass | 38.99 s | 122.08 s | 28 | 2.29 / 14.29 s | `fe56a3eb` | [JSON](durable-closure-2026-09/accepted-8000-model-free-stress.json) |

The measured one-minute host load ranged from 3.80 to 20.94 on 16 logical CPUs.
Per-call timings and summed/union server occupancy remain in each JSON row.
No connector or model-planning duration is inferred from unexplained gaps.

The real-extraction rows precede two bounded follow-ups: model-free custody
verification in the harness and strict catalogue admission for explicitly
requested outside-KB widening. The real-extraction workload does not select
either path. Later rows also include main `a55ce118`, whose additional
product changes affect hosted command binding rather than this stdio workflow.
Each JSON records its exact product source and driver hashes. The final shared
Markdown rows use driver `938e8751`, which corrects canonical path addressing
and string-error classification in the Basic Memory adapter. Earlier rows from
that adapter were invalidated and are excluded.

Every row proves the final direct content, citations, tracker update, stale
relation removal, successful mutations and ordinary recall. Optional graph
warming is allowed only when those mandatory checks succeed.

The 8,000-page real-extraction stress sample remains expensive: 346.55 seconds
with one 149.59-second mutation. Its mutation boundary remained held while
a predecessor-recovery full rebuild published in 143.41 seconds. This
foreground recovery coupling is a remaining performance limitation.
Its seven inserted probes account for about 23.89 seconds directly; subtracting them would miss the changed execution
interleaving. At closure the service recorded 17 graph rebuild attempts, one
completed rebuild and one still active, plus 46,576 path observations in
`find._walk_md`. These are attempts and named-hook observations, not 17
completed full scans. The 3,800-page stress sample finished in 32.97 seconds.
This evidence establishes useful closure, not uniformly bounded latency.
No ratio against the failed baseline or between these single samples is
asserted.

## Shared Markdown diagnostic

This internal diagnostic runs public write, edit, exact read and text-search
operations over identical Markdown inputs. Each product has a fresh home and
state directory, a persistent MCP connection and its recorded configuration.
Semantic search is disabled for both. Basic Memory is the immutable 0.23.2
wheel with SHA-256
`a1679a16319d8a7fb9c0486033551a47dedc0fbae7f5da81444eb3c4bf0ccecb`.
Resolved dependencies are recorded; this is not a claim that its entire
dependency tree was locked by that wheel hash.

| Pages | Product | Result | Verified closure | Startup | Calls | ACK p50 / p95 | Search checks | Evidence |
| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| 3,800 | exomem | Pass | 22.34 s | 70.34 s | 13 | 1.59 / 8.88 s | 0.13 / 0.03 / 0.03 s | [JSON](durable-closure-2026-09/accepted-common-3800.json) |
| 3,800 | basic_memory | Pass | 6.13 s | 15.39 s | 11 | 0.17 / 3.16 s | 0.12 / 0.11 / 0.09 s | [JSON](durable-closure-2026-09/accepted-common-3800.json) |
| 8,000 | exomem | Pass | 54.00 s | 131.57 s | 13 | 2.28 / 17.24 s | 0.50 / 0.29 / 0.26 s | [JSON](durable-closure-2026-09/accepted-common-8000.json) |
| 8,000 | basic_memory | Pass | 6.54 s | 21.68 s | 11 | 0.24 / 2.59 s | 0.17 / 0.11 / 0.13 s | [JSON](durable-closure-2026-09/accepted-common-8000.json) |

The three search-check durations measure each public search through its first
verified expected result; they do not measure indexing delay independently.
The paired runs recorded one-minute external host load from 19.67 to 20.08
on 16 logical CPUs, with task test workers idle. Exact body hashes prove all
four edited/created Markdown bodies. Calls include
product-required preparation and any public convergence polling. No SQL read
or post-write reindex substitutes for a public operation.

The shared row does not exercise evidence immutability, PDF/image extraction
or Exomem's governance and relation-review contracts. Those richer operations
are exercised separately by the Exomem workflow and regression tests; absence
from this adapter says nothing about another product's overall capabilities.
These samples do not satisfy the paired own-harness/direct-row fairness
programme and are not a competitive-suite ranking.

## Rerunning the public workflow

Use an explicit Python interpreter with the selected Exomem checkout and the
runtime dependencies recorded in the sample JSON. Real extraction also requires
PyMuPDF, Pillow, pytesseract and the Tesseract executable. The harness reports
missing extraction dependencies as blocked; model-free results do not claim OCR.

```sh
bench_python=/absolute/path/to/python
bench_run=$(mktemp -d)
XDG_STATE_HOME="$bench_run/runner-xdg" \
EXOMEM_STATE_ROOT="$bench_run/runner-state" \
EXOMEM_CONFIG_PATH="$bench_run/runner-config.json" \
EXOMEM_DISABLE_EMBEDDINGS=1 PYTHONPATH=src \
"$bench_python" scripts/durable_closure_benchmark.py \
  --pages 3800 --profile real-extraction --variant optimized \
  --python "$bench_python" --server-root "$PWD" \
  --state "$bench_run/service" --vault "$bench_run/vault" \
  --artifacts-manifest docs/benchmarks/durable-closure-public-artifacts.json \
  --timeout 180
```

Use a fresh disposable root for every row. Change pages to 8000, select `stress`
for per-write probes, or select `model-free` for preservation/enqueue checks
without extraction claims. The stress rows are independent full runs, not an
optimized run with probe costs subtracted. Keep local test workers idle during
performance samples and record other host load.

For the shared Markdown diagnostic, use the immutable wheel and its matching
installed executable. The complete resolved dependency inventory is recorded in
each JSON; installing the wheel alone does not reproduce that inventory.

```sh
common_run=$(mktemp -d)
XDG_STATE_HOME="$common_run/runner-xdg" \
EXOMEM_STATE_ROOT="$common_run/runner-state" \
EXOMEM_CONFIG_PATH="$common_run/runner-config.json" \
EXOMEM_DISABLE_EMBEDDINGS=1 PYTHONPATH=src \
"$bench_python" scripts/durable_closure_common.py \
  --product both --pages 3800 --timeout 180 --python "$bench_python" \
  --basic-memory-executable /absolute/path/to/basic-memory \
  --basic-memory-wheel /absolute/path/to/basic_memory-0.23.2-py3-none-any.whl \
  --state "$common_run/state" --vault "$common_run/vault"
```

## Review and delivery evidence

Author-independent review exercised the watcher publication race, media child
and parent handoff, exact receipt acknowledgement ordering, pending-catalogue
recall with policy changes, and strict outside-KB widening. The resulting source
and shared-Markdown adapter changes were approved. The adapter's 31-test suite
and an independent run against the pinned Basic Memory wheel passed; the two
paired diagnostic rows above then passed all exact-body and search checks.

Public-artifact validation passed for 3,848 repository files and 3,960 text
payloads. The repository-pinned OpenSpec 1.10.0 validator reported
`185 passed, 0 failed`; archive discipline reported no task-complete active
changes.

The [completion-boundary CI run at `fe56a3eb`](https://github.com/Artexis10/exomem/actions/runs/34116795095)
finished with 69 of 71 jobs passing. Its 32 JUnit artifacts contain 17,345
unique named cases per Python version (3.11 and 3.13), including 302 ordinary
skips per runtime. Python 3.11 had no failure or error. Python 3.13 had one
fixture-setup error; the other failed job was the dependent aggregate gate.

The graph-value fixture waited only 450 ms for a registered rebuild that took
601 ms in CI. Commit `a93e4509` replaces that polling window with a bounded
registered-owner join followed by current publication proof. It preserves
foreign-owner refusals and exact builder failures. The corrected full graph-value
suite reported `163 passed in 221.98s`; independent review forced an 800 ms
owner delay and exercised the refusal/error cases: `7 passed in 36.45s`.
Product source is byte-identical to `fe56a3eb`; the subsequent changes are
benchmark harnesses, tests and evidence. The final-tree CI result is tracked on
[PR #1101](https://github.com/Artexis10/exomem/pull/1101).

Useful durable closure and later full projection convergence remain separate
measurements. Production was not restarted, drained, benchmarked with synthetic
writes or reconfigured. Merge, deployment and OpenSpec archival follow separately
authorized shipping evidence.
