# Design

## Context

See `proposal.md` for the problem and `specs/request-latency-contract/spec.md` for the new behavior.

Current source provides the instruments this change needs:

| Source | Current behavior | Consequence |
| --- | --- | --- |
| `src/exomem/latency_watch.py` | Provisional p90 bars for ordinary and deep recall; minimum 20 samples; trailing day; ring limited to 2,048 calls | Retained samples cannot prove every request in a day or a p95 bar |
| `src/exomem/server.py` | MCP middleware records `total_ms`, leaf `duration_ms`, outcomes, spans and budget | Preserve those meanings and add boundary metadata at this existing seam |
| `src/exomem/server_rest.py` | Command timer starts after ingress checks and before authorization-context verification; success and handled failures carry elapsed time | Extend this command boundary and disclose earlier exclusions |
| `src/exomem/commands.py`, `query_log.py` | Activation leaf records elapsed time, door and outcome in a host-local activation log | Reuse this interval across doors without counting MCP activation twice |
| `src/exomem/request_budget.py` | Absolute monotonic deadlines and pre-stage reserve checks; no interruption of a running stage | A cooperative budget is not proof of a hard request-duration bound |
| `scripts/recall_latency_gate.py` | Existing gate retains the older hybrid ceiling and reads response `timings.total_ms` | Complete `accelerate-governed-recall` task 6.1 rather than adding a competing gate |

Existing bars have separate owners:

| Contract owner | Boundary and population |
| --- | --- |
| `watch-recall-latency-from-the-ledger` | Provisional rolling p90 recall diagnostics, with its own grace and reporting interval |
| Canonical `conversation-aware-activation` | Model-free synthetic conversation stage and warm activation CI profile |
| Active `accelerate-governed-recall` / `recall-latency-contract` | Warm governed recall, served encoder, reference corpus and two-CPU reference runs; distinct live-cell reporting |
| Active `restore-fast-runtime-and-install-parity` / `foreground-latency-slo` | Inner semantic validation and guarded commit on deterministic reference corpora |
| Active `accelerate-durable-write-acknowledgement` / `durable-write-acknowledgement` | Public write, immediate exact read, paired write-plus-read and post-canonical wait; installed acceptance remains open |
| This change / `request-latency-contract` | Live warm activation at the public leaf, separate from dispatch and caller elapsed time |

The active acknowledgement contract and its acceptance tasks remain open. `writer_lease.fast_durable_ack_active()` enables its fast path only when `EXOMEM_FAST_DURABLE_ACK=1`; this change enables no flag.

The implementation must reconcile its final integrated source before changing product paths. Incoming connector-boundary work can alter request cost and authorization checks. Artifact acceptance does not prove that source delivered or its live cost.

## Goals / Non-Goals

**Goals:** Extend existing observations with comparable boundaries, one data source for bars, and honest partial coverage. Attribute runtime costs before changing the paths that pay them.

**Non-Goals:** A second watcher, acknowledgement state machine, benchmark framework, custom validator or model-quality evaluation. This change does not raise the CPU cap, change auth scopes, enable a live flag, or archive unfinished dependencies.

## Decisions

### 1. Package one bar source and load it in existing consumers

Implement `src/exomem/request_latency_contract.json` as the single runtime representation. Use normal package-resource loading and the repository's existing structured validation approach. Each row carries the dimensions required by the spec, its outcome-inclusion rule, an owning OpenSpec requirement reference, and its establishment state. Thresholds use milliseconds; a pending threshold is null. The comparison operator preserves strict versus inclusive bounds.

Profiles and reference inputs are data, not customer categories fixed in Python. The schema fixes only protocol fields and measurement states. Each populated row cites its owning requirement and provenance; consumers import its value instead of reproducing constants. Existing numerical bars migrate unchanged from their current owner. References must follow an owner through archive to its canonical spec.

Keep the live activation bar in this change's requirement. Keep existing bars in their existing requirements. Move the watch's current constants into this representation during implementation, preserving the provisional p90 profile. Do not silently convert those constants into established live p95 thresholds.

The closest maintained option is the existing `latency_watch` with its shared summarizer. Its gap is boundary/profile metadata and coverage accounting, so extension costs less than another metrics or benchmark service. Added maintenance is one packaged data file and small producer adapters. No new dependency is required.

### 2. Keep producer boundaries explicit

| Boundary | Start and end | Producer |
| --- | --- | --- |
| MCP dispatch | Middleware entry through its result or refusal/error result; excludes client transport | Existing MCP call middleware and ledger |
| REST command | Existing command timer through the returned response object; excludes earlier ingress checks and response transmission | Existing REST command facade |
| Public activation leaf | Public activation wrapper entry through finalized packet or refusal/error terminal; excludes outer dispatch and client transport | Existing activation log seam |
| Public mutation leaf | Public product-leaf entry through returned acknowledgement | Existing durable-acknowledgement instrument |
| Inner operation or stage | Named operation/span entry through its completion | Existing foreground and call-span instruments |
| Post-canonical wait | Proven canonical commit through acknowledgement | Existing mutation instrument |
| Immediate read and paired request | Read entry through result, and write entry through that paired read result | Existing acknowledgement instrument |
| Caller elapsed | Installed caller entry through received terminal/packet | Existing installed probes, correlated to origin where available |
| Background freshness | Committed generation through required derived visibility/convergence | Existing receipts and freshness instruments |

Do not relabel a stored `duration_ms` or `total_ms`. If current producers stop before response construction, measure the remaining work and introduce the complete boundary explicitly. A timer cannot measure network delivery it never observes.

Use the existing request correlation context to distinguish intervals. MCP middleware owns its dispatch observation; nested activation owns only its leaf observation. REST activation likewise has different command and leaf intervals. A missing correlation field prevents a joined claim, not the request itself.

### 3. Extend the watch with honest coverage

Extend the existing bounded ring and summarizer. Add operation/profile/boundary identity, outcome and source coverage; keep content out. Count observed calls, eligible timings, refusals/errors, abstentions, unknown timings, partial source reads and evictions over the declared window. Count failed observations separately when no complete sample can be emitted.

Every eligible terminal with known timing contributes to its bar's declared population, including errors, refusals and abstentions, unless its owning contract explicitly excludes it. Store that inclusion rule in the bar metadata and report every exclusion with its owner. A success-only percentile labels its restriction and cannot establish an all-request bar; failure counts alone do not restore the missing slow timings. The new warm live activation bar includes all eligible terminal outcomes. Existing recall warming and contended-sample rules remain unchanged under their owning contract.

The ring reports retained coverage and its oldest retained instant. It never promises a day-wide census merely because the requested window is one day. Doctor can read existing ledger/archive or activation sources off the hot path, but must report their coverage independently. Do not combine sources with different boundaries or missing identities into a seemingly complete population.

Bootstrap keeps its client isolation and bounded advisory presentation. Existing structured logs and doctor expose coverage and pending or insufficient states. No source read, fsync or content capture is added to public request execution. A malformed bar or source produces an unavailable observation, not a request refusal or a fabricated default. This prevents false performance claims; its cost is a missing diagnostic verdict, paid by the operator rather than the caller.

The bootstrap helper reads caller-filtered breaches once. Compact sections and
session project nine fixed advisory fields from those summaries: `tool`, `deep`,
`samples`, `p50_ms`, `p90_ms`, `ceiling_ms`, `bar_id`, `contract_revision`, and
`source`. The bar identity resolves the profile, population, observed-service
reference, window, minimum samples and eligible terminal outcomes through the packaged
contract. Samples count eligible known timings retained in the live ring.
The advisory makes no coverage or installed acceptance claim.

Full and diagnostics bootstrap retain all summary fields except the client,
including every dominant span and coverage. The compact core keeps its breach
pointer, and healthy profiles omit latency. Doctor remains an independent report
over its disk sources and coverage, rather than an exact advisory expansion.
This projection changes no byte ceiling, latency bar or measurement population.
The populated fixture uses producer-shaped dictionary spans and exercises both
detail levels through the same helper.

### 4. Attribute cost before changing runtime behavior

The current activation index update already writes changed rows and signature vectors incrementally. `WorkingSetIndex._write()` still calls `_collect()` and enumerates/stats the corpus; optimize only the measured remaining work. `EmbeddingIndex.upsert_file()` and `_patch_cache()` already splice chunk data; `_unit_vectors()` still reloads the unit matrix after generation changes. `due_state.apply_write_delta()` already exists. Reuse these paths instead of introducing another delta system.

The `service-v1` profile pins the required encoder even when model preload is disabled; matrix caches remain subject to idle reaping. `embeddings.py`, `warmup.py` and `model_reaper.py` implement this policy. Choose any residency adjustment from measured cold/warm cost, RSS and the served profile. Older idle-CPU and release-latency records cannot establish today's baseline. Live OAuth churn and its cost remain unverified until current attributed evidence exists.

First prove provider/index rows and sampled identities, then public boundary coverage, then stage and CPU attribution. Retain `accelerate-governed-recall` task 6.1 as the recall gate work. Attribute remaining work with the existing instruments before making a targeted cost repair. Reuse exact durable receipts, async terminal custody and pending recall; defer only work their current contracts permit.

### 5. Preserve deadline and acknowledgement ownership

Budgeted read-only activation must return a finalized packet or its existing budget abstention within the existing absolute deadline. Packet finalization is inside that deadline. An overrun is a failed deadline observation and leaves the repair unresolved, even if the next stage yields. Canonical mutation immunity does not apply to read-only activation.

First localize the overdue interval through a bounded isolated reproduction. Reuse `RequestBudget` checks within controllable loops and waits, and existing safely deferred paths. Managed index refresh already defers its rebuild in `working_set_runtime.refresh_index()`; retain that path. Preserve current skip/truncation and budget-abstention payloads. Do not add killable threads or a watchdog framework. If the responsible operation has no safe bound, keep the repair open while independent observation and bar-loading work continues.

Canonical commits remain immune to cancellation. All existing terminal, idempotency, fencing and committed-uncertain contracts apply. Preserve exact affected paths, required component sets, derived batch custody and pending-visibility rows. This change does not choose another acknowledgement framework or bypass a fail-closed read when visibility cannot be proven.

### 6. Prove the delivered workflow with existing instruments

Use isolated installed before/after arms on identical inputs and reference state. Bind every run to installed bytes, runtime/embedding profile, SQLite, corpus counts, auth population, CPU cap, memory and window. Verify counts and sampled identities from input to stored rows to returned packets before deriving percentiles. Record exclusions and contention under the owning contract.

Report case-paired differences and their confidence intervals, plus CPU, RSS, failures, quality, freshness and packet/result identity separately. An interval including zero shows no measured change. Model-free CI proves its declared profile only. Runtime fixes require scoped regression checks; reuse the existing full corpus at product delivery. This artifact batch runs OpenSpec and privacy checks only.

### 7. Bound the retrieval carry by the turn and the rarity cap

Attribution put most abstaining activation time in the retrieval carry, and its cost grew with the knowledge base. Three mechanisms caused this, and each is now bounded without changing an answer:

- A word's rarity count over the admitted pages stops at `cap + RETRIEVAL_CARRY_FETCH + 1` qualifying rows. Past that window no count changes a decision, so the scan no longer reads every page that holds a common word.
- The owner's phrase rung ranks on catalogue statistics, as the title rung already did. It no longer rebuilds Okapi statistics from every admitted page after each catalogue write. A restricted reader still ranks on statistics over its admitted pages, and admission is still decided on every request.
- A corroborated ranking checks rows in rank order and stops once it holds its rows. When phrase groups are the whole test, the index names the rows that can pass first, on `+fts.rowid` so that the bm25 figures stay those of the one MATCH.

Every carry rung records a span with its admitted pages, scanned stem rows, examined candidates and a statistics-rebuild count. Every role lane records its catalogue rows, parsed pages and the time inside each read. These spans give the measurement for this decision.

**Declared behaviour change.** The owner's phrase rung now orders pages by the catalogue's bm25 scores, not by Okapi scores over the admitted corpus. Scores move, so an owner's packet can differ in three ways:

- the order of named domains, and therefore which three are carried, when more than three phrases each name one page;
- the order of the pages an abstention lists at `retrieval_named`;
- which current pages fit in a carry window crowded by retired pages, which can change whether a phrase names one page or two.

Restricted readers' rankings do not change. Some corpus-sized work remains, all of it in SQLite rather than in the carry's own loops. The request's admission still reads every page's path. Catalogue ranking still reads every row that holds a turn word, and a write still invalidates the catalogue's cached word counts. The model-free gate in `tests/test_latency_gate.py` admits this growth and refuses whole-corpus work.

## Risks / Trade-offs

- Retention or observation failure can bias percentiles → Expose partial coverage and forbid complete-population claims.
- Outer work can move outside an inner timer → Preserve boundary identities and compare caller, dispatch and leaf intervals separately.
- Deferred work can hide CPU or freshness cost → Keep their evidence separate and preserve exact custody and immediate-read checks.
- Residency can increase memory → Choose from current RSS/cold/warm measurements under the existing CPU cap.
- Contract references can outlive active-change paths → Update references through archive without duplicating numerical bars.

## Migration Plan

1. Reconcile the final source and current owning contracts; record any incoming boundary change before product edits.
2. Prove data identities and public coverage on isolated inputs; preserve an installed baseline with its declared scope.
3. Package the bar source and extend existing consumers; retain the existing instruments and pending states.
4. Complete the existing recall gate slice and attributed runtime fixes; verify equivalent results, custody and authorization.
5. Compare installed arms on identical inputs; record confidence intervals, CPU, memory, quality and freshness.
6. Release and install through existing operational ownership; verify live state read-only with existing release gates.

For an actual live setting or runtime switch, record its owner, preconditions, undo and post-switch checks before applying it. No switch is required to accept these artifacts. Rollback uses the previous installed revision and previous settings; exact terminals and receipts retain custody across recovery. Client-production writes remain with the operator.

## Open Questions

Read, browse, deep, cold and background-freshness thresholds remain pending where no current applicable bar exists. Current measurements will support their recorded engineering choices. Residency selection remains conditional on the served profile's measured memory and cold/warm costs. Neither unknown grants a passing result or introduces another approval or release gate.
