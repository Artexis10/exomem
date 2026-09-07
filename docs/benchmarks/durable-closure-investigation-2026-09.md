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

## Acceptance boundary

Production was not restarted, drained, benchmarked with synthetic writes or
reconfigured during this investigation. The implementation requires independent
review, isolated public-workflow measurements at approximately 3,800 and 8,000
pages, and the repository's completion-boundary test run before delivery. Useful
durable closure and later full projection convergence are separate measurements.

A Basic Memory comparison uses its immutable 0.23.2 wheel and persistent public
MCP operations over the shared Markdown subset. It is an internal diagnostic,
not a competitive-suite ranking; evidence immutability, media processing and
governance are reported separately from the common operations.
