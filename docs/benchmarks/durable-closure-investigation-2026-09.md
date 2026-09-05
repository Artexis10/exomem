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

### Optional graph expansion vetoes useful direct recall

Managed ordinary retrieval already has maintained catalogue admission and an
exact pending-write overlay. However, its optional graph expansion calls a
strict current resolver. A typed resolver-warming exception escapes candidate
collection and discards otherwise eligible lexical/vector matches.

The regression exercises all four resolver-warming sites in both branches:
unavailable graph fallback and available graph with unindexed legacy seeds.
The repair omits unproven graph contributions and discloses graph warming. It
does not relax catalogue, policy, pending-visibility or relation-predicate proof.

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
