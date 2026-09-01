## Context

See `proposal.md` for motivation and measured totals. The current governed-write
shape is:

1. semantic and governance preflight;
2. a guarded atomic canonical batch;
3. synchronous `post_commit_batch_fanout`, whose component dispatch is ordered
   memory references → resolver → semantic purge → lexical catalogue → graph →
   embeddings;
4. for compiled creation, a second post-commit duplicate/overlap advisory pass;
5. terminal persistence and response.

The canonical files are already durable at step 2. The current deferred-index
store has CAS-revisioned semantic, graph, and full-upsert queues, bounded/fair
drains, and a prompt graph-drain daemon. `full_upsert_succeeded` already accepts
an exact durable semantic or graph deferral and rejects uncovered deferral. The
missing pieces are a crash-safe pre-commit batch envelope, a normal writer route
that uses component custody rather than synchronous completion, exact pending
read visibility, and advisory custody.

Several active changes are architectural prerequisites, not scope to absorb:

- `shorten-mutation-critical-section` moved validation/model loading outside
  canonical authority and is shipped but still carries closure evidence debt.
- `fix-deferred-work-drain`, `converge-graph-incrementally`,
  `rebuild-graph-without-blocking-writes`, and
  `bound-graph-recovery-funnel` own convergence, graph recovery, and their live
  acceptance.
- `bound-corpus-context-flight-join` owns the remaining unbounded interactive
  corpus-context wait. Before delegation it must pin the already measured 2.0 s
  caller bound and the zero-work fast path; this change assumes that contract.

Implementation does not begin on a file shared with unfinished prerequisite
work until the prerequisite is merged/reconciled and its OpenSpec delta is
either archived or explicitly sequenced ahead of this branch.

## Goals / Non-Goals

**Goals:**

- Make the common acknowledged write a small canonical transaction plus exact
  custody, independent of graph/vector/advisory duration.
- Close the canonical/derived-store crash cut without treating derived state as
  proof that the canonical mutation succeeded.
- Preserve exact direct and lexical read-your-write while slower projections
  converge.
- Reuse one generation's vectors across embedding publication and advisory
  comparison.
- Make every deadline, pending state, failure, and backlog observable without
  vault content in telemetry.
- Produce lane-ready red-first tests, mutation proofs, and deterministic gates.

**Non-Goals:**

- Changing semantic/governance acceptance, canonical Markdown, graph topology,
  retrieval ranking weights, or the meaning of a compiled note.
- Making explicit `suggestions=true`, reconcile, or operator index commands
  subject to the interactive fast-acknowledgement SLO.
- Replacing SQLite, rewriting in Rust, adding a server-side reasoning model, or
  depending on GPU availability for correctness.
- Claiming that vector or graph recall is current before its exact generation is
  published.
- Replaying an ambiguous canonical mutation from a derived receipt. Existing
  idempotency and GraphCommitReceipt authority remains the only canonical
  terminal authority.

## Decisions

### 1. Add a prepared batch envelope to the existing deferred store

Evolve the machine-local `.deferred-index.sqlite` family additively instead of
creating a second scheduler or queue database. New normalized tables carry:

- `derived_batches`: version, opaque batch id, mutation attempt/token digest,
  canonical generation/checkpoint identity, state, timestamps, and bounded
  failure code;
- `derived_batch_paths`: safe canonical relative path, exact before hash or
  absence, intended after hash or tombstone, and optional stable memory ref;
- `derived_batch_components`: closed component enum, CAS revision, state,
  claim owner/expiry, attempt count, next-attempt time, and bounded outcome;
- `pending_recall_rows`: the bounded identity/search projection needed for the
  current generation until persistent catalogues prove publication, plus an
  opaque store-generation fence advanced by every insert, update, or delete;
- `write_advisory_results`: stable opaque result id, batch/component revision,
  exact target path/fingerprint, closed status/code, retention deadline, and
  CAS publication state;
- `write_advisory_result_candidates`: an ordered, bounded child set carrying
  counterpart identity/fingerprint, warning, advisory ref, review ref, and
  triage fingerprint without packing multiple candidates into one column.

The closed component vocabulary for the first release is `freshness`,
`memory_refs`, `resolver`, `semantic_purge`, `lexstore`, `graph`, `embeddings`,
`claims`, and `write_advisory`. Optional components are recorded as
`not_required` when disabled or inapplicable; absence never means completion.
Paths/hashes/identities are machine-local operational state and no Markdown or
arbitrary metadata is copied into the receipt.

The old semantic/graph/full tables remain readable during migration. Their
drainers continue until empty; new fast writers create only batch/component
rows. This avoids rewriting thousands of legacy entries and allows a safe
mixed-version rollout.

**Alternative rejected — add semantic/graph rows only after commit.** That is
cheap, but a process death between canonical replacement and SQLite insertion
loses the exact work demand and recreates the defect this change is meant to
remove.

**Alternative rejected — put receipts in the synced vault.** They are
machine-local derived-work custody, not portable canonical authority. Synced
operational artifacts create cross-replica ambiguity and watcher churn.

### 2. Prepare first; prove rather than activate

Under canonical mutation authority, after all content and guards are known but
before the first replacement, the coordinator inserts one `prepared` batch and
all path/component rows in one SQLite transaction. The receipt binds the same
canonical generation/checkpoint selected by the batch and a digest of the
current mutation attempt; it does not expose arguments or content.

No post-commit `activate` write is required for correctness. A worker or the
original request evaluates a prepared batch from source of truth:

- every path equals its intended after hash/tombstone and generation matches →
  transition components to `ready`;
- every path equals the before state and the canonical attempt is known not to
  have committed → retire as `aborted`;
- a newer exact batch covers the same path/component and current canonical
  generation → retire as `superseded` only after the newer pending-recall row is
  live;
- mixed or otherwise unprovable state → `reconcile_required`, never publish.

Caught rollback attempts to CAS-retire the prepared row while authority remains
held, but safety does not depend on cleanup succeeding. A stale prepared row
cannot satisfy its after-state proof. A crash after canonical commit needs no
activation bit: the durable intended hashes are already enough to resume
derived work, while the existing idempotency/GraphCommitReceipt protocol decides
canonical retry semantics independently.

Publication is at-least-once with exact generation checks and idempotent sidecar
upserts. Component completion CAS-clears only the claimed revision. Claim expiry
allows another process to resume after worker death.

**Alternative rejected — infer commitment from filesystem similarity for the
mutation terminal.** The receipt authorizes only derived convergence; it never
heals or replays canonical idempotency. The unavoidable terminal crash cut stays
fail closed.

### 3. Keep one small synchronous acknowledgement cut

The acknowledgement sequence is:

1. existing semantic/governance preflight and under-authority revalidation;
2. prepare the exact derived batch;
3. commit the complete canonical batch, graph floor/checkpoint, Markdown indexes,
   log, back-references, and governance catalogue effects under existing guards;
4. publish the O(changed paths) pending-recall delta and signal component drains;
5. persist the exact canonical mutation terminal;
6. return immediately if required visibility is proven; otherwise observe at
   most one shared deadline ending 2.0 s after canonical commit and return the
   closed pending/failure state.

The request thread never calls a model or starts a vault-sized rebuild. It may
perform bounded path-local parsing and publication required to make the pending
delta visible. Any optional observation of already-running component work uses
the same absolute deadline; helper APIs receive `deadline_monotonic`, not a
fresh duration, so nested calls cannot multiply it. A zero-work check precedes
all waits.

The 2.0-second number is fixed and internal. It is chosen from the caller budget,
just above the existing 1.5-second commit p95 ceiling, not from model or rebuild
duration. A temporary rollout kill switch may restore the old wide synchronous
fanout, but there is no knob that raises the fast path's deadline.

**Alternative rejected — two seconds per component.** Sequential component
timeouts turn a bounded design back into a 10–20 second call.

**Alternative rejected — launch ordinary threads and return.** A thread is not
durable custody; process restart loses it, and unbounded thread creation makes a
burst a resource incident.

### 4. Pending recall is an overlay, not a claim that every index is current

The receipt transaction contains only bounded path/hash/ref facts. After
canonical commit, the request publishes an in-memory `PendingRecallDelta` by
reading/parsing only the changed paths and validating their exact after hashes.
It contains current searchable fields needed by keyword/hybrid recall and
tombstones for removals. This is O(changed paths), not a corpus walk.

Recall uses the delta as follows:

- direct path reads continue reading canonical Markdown;
- stable-ref resolution consults current pending ref/path mappings before the
  persistent memory-ref sidecar;
- keyword/hybrid search merges current delta pages with the last lexical
  catalogue and removes all catalogue/vector/graph rows for paths or generations
  shadowed by pending updates/tombstones;
- the same canonical identity is deduplicated after a move;
- vector and graph lanes contribute only proven current generations; otherwise
  their pending coverage is disclosed in the default retrieval status.

An overlay row is removed only after the matching persistent component proves
the exact after generation. There is no removal-before-publication gap.

At process start, managed recall hydrates the overlay from outstanding receipts
before it declares readiness. Hydration is bounded. If the pending set exceeds
the bound or any exact path cannot be proven, recall returns the existing typed
warming/unavailable outcome while a background pass rebuilds the overlay. It
never silently serves a stale last-published catalogue. Offline callers retain
their existing source-walk fallback.

The receipt store exposes this restart path through bounded typed operations,
not private SQLite access: a snapshot returns every non-retired pending batch
with its exact receipt, path generation/revision, state, and an opaque store
generation; a fence operation proves that generation is still current; and an
exact CAS retirement operation clears only rows from that same batch,
generation, and revision. Snapshot outcomes are closed as `complete`,
`overflow`, or `unprovable`. Only a complete snapshot whose generation remains
current can authorize managed recall readiness. Any concurrent pending-row
mutation invalidates the fence and forces another bounded hydration attempt.

**Alternative rejected — rely on watcher echo.** Watchers are optional, delayed,
and deliberately suppress self-authored echoes. They remain a backstop, not the
read-your-write handoff.

**Alternative rejected — report success and accept temporary search absence.**
The returned path is not enough for an agent that immediately verifies by
stable ref or search, and stale pre-edit excerpts are worse than an explicit
warming outcome.

### 5. Component drains reuse existing ownership and fairness

There is one scheduling owner per component family, reusing the current watcher
reconcile, graph drain, resource mode, model guards, and CAS receipt mechanics.
No new free-running scheduler competes with them.

Dependency order inside one batch is:

1. prove canonical generation and publish pending recall;
2. semantic purge and identity/resolver projection;
3. lexical publication;
4. graph, embeddings/claims, and advisory work, parallel where their existing
   resource guards allow it;
5. retire the pending overlay only after every shadowed persistent lane needed
   for ordinary recall proves the generation.

Graph and embedding receipts use the existing exact-coverage carve-outs in
`full_upsert_succeeded`. A new component result cannot be called durably deferred
unless the batch/component revision exists. Full receipts remain escalation for
unclassified/uncovered failures only. Each pass is bounded; failures rotate
fairly and back off. Quiet mode throttles but never halts correctness work.

### 6. Advisory rides the embedding generation

The default duplicate/overlap sweep becomes `write_advisory` component work. For
an applicable compiled page the embedding worker prepares the exact chunks and
vectors once. Before or while publishing those vectors it computes best-per-file
cosines excluding the target identity, then feeds the existing deterministic
duplicate/overlap thresholds and review-state suppression. If vector rows were
published before a worker crash, replay reads the exact current page vectors
from the sidecar and excludes self instead of re-encoding.

Advisory output remains noncanonical and fail-open with respect to the committed
write. The compact terminal returns a stable opaque
`exomem://write-advisory-result/<id>` reference. Exact
`review_memory(mode="write-advisory-result", ref=...)` lookup returns only
`pending`, `ready`, `failed`, or `superseded`; it has no list form and never
joins attention, due-state, bootstrap, or a later response. Ready output carries
the existing fingerprint-bound write-advisory refs and bounded warning strings.
Every lookup rechecks target/counterpart fingerprints and the current release
plane; withheld candidates are indistinguishable from absence. Failure stays
visible in exact result status, derived diagnostics, and retry telemetry; it
cannot change `status=committed`.

The receipt store owns additive typed publication and exact-read operations for
these results. Publication requires the exact claimed `write_advisory`
component revision, lease owner, lease expiry, target path, and target
fingerprint; it atomically CAS-publishes either a bounded ordered candidate set
or a closed failure code. Identical replay is idempotent and conflicting or
stale replay is refused. A crash after result publication can therefore reuse
the stored result and complete the component without recomputation. Old rows
without the additive target identity remain resolvable by their stable ref but
fail closed with a fixed compatibility code and no candidate payload.

`suggestions=true` remains synchronous because it is an explicit request for
the enriched related-link result in the current response. It is not silently
converted to background output. It retains the current pure-substrate boundary:
retrieval models rank and deterministic code surfaces candidates; no model
authors, edits, triages, or decides canonical knowledge.

**Alternative rejected — keep the default advisory inline and merely share one
encode.** Matrix loading/search and review-state I/O still measured 6.76 s in a
representative call. Reuse removes duplicate compute but cannot supply a hard
interactive bound by itself.

### 7. Terminal and current status remain separate

The persisted compact terminal adds closed top-level outcomes while preserving
the existing `graph_sync` contract:

- `derived_sync`: `completed | pending | failed`;
- `derived_sync_components`: a bounded sorted list of pending/failed non-graph,
  non-advisory component names when not completed;
- `advisory_sync`: `completed | pending | not_required | failed`;
- `advisory_result_ref`: the stable opaque exact-lookup reference when advisory
  work exists, plus a closed failure code/fixed next action when failed.

No paths, model errors, arbitrary messages, or queue internals appear in compact
state. Full diagnostics may include bounded stable codes, batch id, component
ages, and remediation. The terminal records what was true at acknowledgement
and never changes. A later status/recall/doctor call reports current convergence;
an exact mutation retry replays the original terminal.

`pending` is only a deadline/custody outcome. Receipt preparation failure,
canonical proof failure, worker registration failure, or publication failure
retains its real error code. Optional advisory failure is a failed optional
component, not mutation failure.

### 8. Measure the user-visible chain and mutation-proof every guard

Extend call-ledger phases and metrics with content-free measurements for receipt
prepare/proof, canonical commit, pending-visibility publication, acknowledgement,
per-component queue age/depth, component completion, and advisory vector reuse.
The existing write timing script gains public-leaf acknowledgement and immediate
stable-ref/keyword/hybrid read rows; boundary, validate, and commit rows remain so
regressions can be localized.

Red-first failure injection covers every cut:

- delete receipt preparation → the test must fail by allowing a canonical write;
- delete after-hash proof → the test must fail by publishing rolled-back/stale
  derived rows;
- delete completion CAS → the test must fail by clearing a newer revision;
- delete overlay shadowing → the test must fail by returning stale pre-edit or
  deleted content;
- replace the shared absolute deadline with per-component durations → the test
  must exceed 2.0 s;
- restore inline advisory encode → the default public-write latency test must
  block on the injected slow encoder;
- remove startup overlay hydration → the restart read must serve stale data or
  warming, and the test requires warming until exact coverage is ready.

Mutation tests run in harness-owned scratch copies with imports proved to come
from those copies. Real-scale benchmarks run only on a quiesced machine and only
once at the delivery boundary; focused deterministic gates run in each lane.

### 9. Freeze lane interfaces before parallel work

Lane 1 owns the only cross-lane protocol module. The writer/terminal handoff
subset is `prepare_batch`, `prove_committed`, `publish_pending_visibility`,
`signal_components`, `component_status`, and `advisory_result_ref`. The store
consumer subset additionally exposes bounded pending-visibility
snapshot/fence/exact-retirement and advisory-result publish/exact-read seams.
Their production types and fakes are part of the frozen foundation; child lanes
must not infer them through private SQLite access or invent a second store.

Lane 2 implements the pending-visibility publisher and recall consumer using
the bounded snapshot/fence/retirement subset. Lane 3 calls only the frozen
writer/terminal handoff, supplies an advisory target path when custody applies,
and tests all writer routes with the Lane 1 fakes, so it does not depend on Lane
2 or Lane 4 source. Lane 4 implements advisory component execution and exact
result resolution using the frozen publish/read subset without editing
writer/terminal or receipt-store files. Cross-lane production wiring and public
write→read/result tests belong only to Lane 5.

The first accepted Lane 1 lifecycle commit is followed by one additive,
independently reviewed foundation-extension commit before any child lane is
accepted. That extension is restricted to `deferred_index.py`,
`derived_receipts.py`, `test_derived_batch_receipts.py`, and
`derived_receipt_fakes.py`; it preserves the accepted lifecycle commit and adds
the missing store-consumer contracts and mixed-version migration.

The executable DAG is therefore:

1. Lane 1 receipt lifecycle, then the accepted additive foundation extension;
2. Lanes 2, 3, and 4 in parallel from the accepted extended Lane 1 SHA;
3. Lane 5 integration, instrumentation, benchmarks, and rollout evidence from
   the three accepted author-lane commits.

Lane 5 is itself an authored lane, not unreviewed orchestrator glue. Its fresh
reviewer examines both the merge and every instrumentation/integration byte.
No author may review their own lane, and correction commits return to the same
fresh reviewer for recheck.

### 10. Pin mutation proofs to named guards

| Guard | Deliberate mutant | Test that must turn red |
|---|---|---|
| Receipt before canonical replacement | Remove `prepare_batch` | `test_receipt_prepare_failure_leaves_canonical_untouched` |
| Exact after-state activation | Accept a hash/generation mismatch | `test_mixed_or_stale_state_never_activates_components` |
| Supersession/completion CAS | Let an old revision clear the current row | `test_older_revision_cannot_clear_newer_custody` |
| Pending stale-row shadow | Return persistent rows before applying tombstones | `test_pending_edit_delete_hides_stale_rows` |
| Overflow/warming fail-closed | Serve the old catalogue after incomplete hydration | `test_unprovable_pending_overflow_returns_warming` |
| Authority release | Keep mutation authority during a slow component | `test_slow_component_runs_after_authority_release` |
| Shared deadline | Give each component a new duration | `test_components_share_one_absolute_post_commit_deadline` |
| Failure truthfulness | Convert registration/proof failure to pending | `test_real_custody_failure_is_not_laundered_to_pending` |
| Default advisory isolation | Restore inline encoding/comparison | `test_default_write_never_encodes_advisory_inline` |
| Advisory authorization | Bypass lookup-time release projection | `test_advisory_result_withheld_candidate_is_absent` |
| Advisory queue isolation | Inject a result into attention/due-state | `test_advisory_result_never_joins_review_carriers` |
| Vector reuse | Encode the same generation for both consumers | `test_embedding_and_advisory_encode_generation_once` |
| Corpus-flight no-op | Remove the settled/no-flight early return | `test_settled_or_absent_corpus_flight_never_waits` |

Each lane packet names every applicable row, imports from a harness-owned copy,
applies one mutant at a time, records the expected failing node, restores the
accepted source, and reruns the same node green. A lane with an unproved row is
not eligible for review.

## Risks / Trade-offs

- **[Risk] The prepared receipt widens the under-authority critical section.** →
  One bounded SQLite transaction contains hashes/paths already computed for the
  batch; latency gates separate receipt preparation and fail if it scales with
  corpus size.
- **[Risk] A pending overlay becomes a second search index.** → It is a bounded
  delta over unfinished receipts, never a corpus copy; persistent catalogues
  remain primary, and overflow fails closed to warming.
- **[Risk] Later writes race older workers.** → Every publication and completion
  is generation-bound and CAS-revisioned; stale work can run but cannot become
  current or clear newer custody.
- **[Risk] A queue backlog makes recall warm indefinitely.** → Prompt drains,
  periodic reconciliation, age/depth alarms, fair rotation, and live acceptance
  prove convergence after a burst. Warming is preferable to stale answers when
  the overlay cannot be complete.
- **[Risk] Async advisories arrive after the writing conversation.** → The
  terminal carries an exact opaque result reference; results never appear in an
  unrelated conversation, attention, or due-state. Explicit `suggestions=true`
  remains available when same-response enrichment is worth the latency.
- **[Risk] Optional embeddings are disabled or fail.** → Vector, claims, and
  embedding-dependent advisory components report disabled/not-required/failed
  under existing soft-fail rules; canonical write and lexical read-your-write do
  not depend on a model.
- **[Risk] Mixed old/new releases strand receipts.** → Schema changes are
  additive, old queues remain drainable, new behavior is enabled only after the
  new reader/worker is live, and rollback first restores wide synchronous fanout.
- **[Risk] The plan collides with unfinished graph/deferred work.** → Lane intake
  refuses shared-file implementation until prerequisite branches are reconciled;
  integration order is explicit in `tasks.md`.

## Migration Plan

1. Reconcile prerequisite OpenSpec changes and land the 2.0-second corpus-flight
   fast-path pin. Record a clean base revision.
2. Add the receipt schema, proof/recovery logic, diagnostics, and legacy-queue
   compatibility with `EXOMEM_FAST_DURABLE_ACK=0`. Deploy/read it without
   changing writer behavior.
3. Dual-record prepared batch receipts while retaining synchronous fanout.
   Prove rollback/crash cuts and that receipts retire without double work.
4. Enable the pending-recall overlay and prove create/edit/delete/move
   read-your-write and restart warming behavior.
5. Switch derived fanout to receipt-owned background execution for a bounded
   command tranche, then the remaining governed mutations. Set
   `EXOMEM_FAST_DURABLE_ACK=1` only after dual-record and pending-read evidence is
   green; the shipped default changes to `1` only after the live acceptance.
6. Move default advisories behind custody and enable vector reuse. Keep
   `suggestions=true` synchronous and explicit.
7. Run one quiesced realistic-scale Linux gate, one native Windows installed
   product/live-cell acceptance, restart/crash probes, strict OpenSpec
   validation, privacy, and full suite. Observe pending age/depth and response
   percentiles through a staged rollout.
8. If rollback is required, set `EXOMEM_FAST_DURABLE_ACK=0` first, allow the new
   worker to drain or export exact outstanding status, then roll code back.
   Disabled mode intentionally restores the prior wide synchronous behavior and
   declares the fast-ack capability/SLO inactive; it is not a configurable
   extension of the enabled path's fixed 2.0-second budget. Additive tables remain
   inert and are not deleted by rollback.

Rollback is mandatory on any stale post-write read, any acknowledged write with
missing required custody, any cross-tenant/result authorization leak, two or
more new committed-uncertain outcomes in 15 minutes above a zero-event canary
baseline, any covered write minting an uncovered full receipt, oldest required
component age above five minutes for three consecutive one-minute observations
in normal resource mode, or server/connector p90 above five seconds for three
consecutive 30-call windows. Expansion pauses on the first threshold breach;
the safety/privacy cases disable immediately rather than waiting for repetition.
