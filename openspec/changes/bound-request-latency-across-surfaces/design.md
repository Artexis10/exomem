# Design

The programme uses one packaged MCP observer for positive `ask_memory`, `activate_context` and `read_memory` cases. The existing recall script delegates its MCP mode and retains its other diagnostics. Private positive cases declare read arguments, expected state, required references and material. FastMCP measures each call through parsed response validation. The observer reuses `latency_watch`, the packaged contract and one report schema.

Case expectations retain the existing indexed-path representation and add a strict `response_schema` alternative. A case cannot mix the representations. The alternative uses the existing `jsonschema` dependency with Draft 2020-12 and validates the complete parsed response. The runner compiles validators before connecting and keeps response validation inside the measured interval. Schema resources use an empty `referencing` registry without retrieval; local references work, while unavailable external references fail locally. Conflicting dialect declarations in schema positions are invalid; dictionaries inside data-valued assertions retain their literal meaning.

An independent source-only author establishes each key's source identity, state and material requirements. One witness must bind source identity and nonblank material to the same result or parent. Membership does not imply rank; keys require rank only when source truth establishes that contract. The author checks missing identity, wrong identity, blank material and material attached to another parent. Generic schema validation proves schema syntax and the supplied assertions, not that a key establishes these semantic obligations. Trusted private schemas retain synchronous evaluation; the asynchronous timeout does not establish a schema CPU deadline.

Schema mismatches report `unexpected-response`. Schema execution or resolution errors report `invalid-expectation`, retain the parsed outcome and stop the series. Such an observer error leaves availability and coverage incomplete and fails the check; it does not establish a service outage. Reports and warnings omit private schemas, constants, responses and exception details. Existing legacy failure codes remain unchanged. Frozen keys and failed series remain unchanged; independently authored replacement keys start a new series and preserve unused cases' blindness.

An attempt can retain `schema_failure` from the first mismatch in that same validation pass. It contains only the validator keyword and a SHA-256 of the schema path encoded as compact JSON. The fingerprint supports correlation within the frozen case and observer version. Different assertions can share a validator path through references or composition. The fingerprint does not identify a unique assertion, establish its product cause or enumerate every failure.

Raw paths, instance paths, messages, schemas, constants and responses stay absent. Hashes permit correlation and do not guarantee secrecy. Unavailable diagnostic metadata cannot change an established mismatch.

The command selects availability or acceptance. Availability checks every configured case once and requires positive coverage for each represented tool. Its successful result can coexist with insufficient latency evidence. Acceptance retains the declared sample, warmth, contention and deployment requirements. Observer identity remains separate from deployment identity. A deployment change invalidates previous acceptance.

A standard systemd user timer runs the availability check through an explicitly selected installed observer. The observer remains independent of the worker interpreter and upgrade path. One bounded oneshot unit prevents overlap; the application adds no scheduler or repair loop. Private configuration supplies existing credentials. Missing authorization leaves installation unconfigured; installation never issues a grant. The latest report is atomic and owner-only.

A failed unit, structured warning and offline doctor status provide local visibility. They do not deliver a push notification. Doctor distinguishes missing configuration, disabled scheduling, failed execution, stale evidence and fresh results. An old successful report cannot conceal a later failed execution. Notification delivery remains open until an authorized channel exists and delivery is verified. Production probes stay read-only. Natural write observations and disposable write acceptance retain separate provenance.

## Ordinary write follow-through

Write advice reuses the page delta's projection and serves only affected paths. Its optional `scope` is `affected_paths`; omission preserves historical global meaning. Global and affected-path blocks retain separate emission history. Delivered rows already carry their category and fingerprint, so first-surfaced recording uses those identities without scanning the projection.

Flush receipt proof and pending publication newest-first before the outermost matching vault hold releases. Re-prove earlier session receipts whose paths overlap newer commits. Keep component waits and terminal persistence outside the hold. Record handoff failure as derived work pending, without changing the committed outcome. Direct leaves without a matching hold retain the guarded post-terminal path.

Prime the broad writer resolver from the proven structural corpus after semantic warmup, with existing freshness checks. Ordinary recall retains its separate projection. Blocked edits never warm shared writer state. The existing cache and receipt machinery supply these capabilities; no new scheduler or readiness gate is needed.

A carried corpus re-proves its retained census after the serving watcher seeds a new checkpoint. The existing warmer reuses unchanged pages and reconciles missed edits. An incremental publication captions the corpus only when retained history bridges its prior caption and covers every changed Markdown path.

Manifest revisions refresh the touched claims projection before its served view is reused. Egress retains its independent release checks. Existing spans carry content-free CPU measurements where wall time alone cannot distinguish work from waiting.

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

The existing HTTP access event adds worker entry-to-access and entry-to-final-body intervals.
A scope stamp precedes FastMCP authentication and carrier handling; the existing send wrapper records successful final-body handoff.
The original access duration still ends when the inner ASGI application returns.
Missing entry or final-send evidence remains null, and a completed final send remains known despite later cleanup failure.
The intervals overlap and exclude manager forwarding and client receipt; streaming duration describes stream lifetime.
Early refusals before access retain their existing lack of an access event.

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

Due advice reuses the existing bounded content-token memo. Projection emission and review first-surfacing metadata do not change its served content. Every other review field, including future fields, remains part of its identity. A miss binds rows to the validated review payload that it hashes. An invalid review store retains the existing silent advisory result. The memo records a file identity only when that identity stays unchanged across the read.

The `service-v1` profile pins the required encoder even when model preload is disabled; matrix caches remain subject to idle reaping. `embeddings.py`, `warmup.py` and `model_reaper.py` implement this policy. Choose any residency adjustment from measured cold/warm cost, RSS and the served profile. Older idle-CPU and release-latency records cannot establish today's baseline. Live OAuth churn and its cost remain unverified until current attributed evidence exists.

First prove provider/index rows and sampled identities, then public boundary coverage, then stage and CPU attribution. Retain `accelerate-governed-recall` task 6.1 as the recall gate work. Attribute remaining work with the existing instruments before making a targeted cost repair. Reuse exact durable receipts, async terminal custody and pending recall; defer only work their current contracts permit.

Due-state write cost (tasks 3.2 and 3.3), measured on a large personal vault with the service's request ledger over 2026-10-18 18:05 to 2026-10-19 14:23 UTC (releases 0.127.5 and 0.128.0, compiled-page writes only): `post_commit.due_state` had n=82, p50 1368 ms and max 6805 ms; `post_commit.observation_delta` had n=67 and p50 682 ms; `post_commit.records_routing` had n=82 and p50 399 ms. The machine-local projection was 21.6 MB, and 93% of it was `collection_candidate` entries that embed page-unit copies. A JSON parse of that file took 484 ms and its serialization took 392 ms. Two mechanisms explain the spans. First, `save` never refreshed the shared parse cache, so the first read after every write parsed the whole file again. Second, every governed write rewrote the file, because the projection carries a copy of the emission ledger whose write tick changes on every write, although the sidecar already holds the ledger of record. The repair keeps both paths and the file format. `save` now keeps the payload it wrote under the identity of the bytes it wrote, and drops that entry if the destination shows a different identity. A delta write skips `save` when only the emission copy differs, still ticks the sidecar, and compares only the entries the delta replaced. The collection-candidate representation is a separate follow-up. `op_preserve` records the existing `post_commit.records_routing` and `post_commit.observation_delta` spans so the next measurement covers that path.

### 5. Preserve deadline and acknowledgement ownership

Budgeted read-only activation must return a finalized packet or its existing budget abstention within the existing absolute deadline. Packet finalization is inside that deadline. An overrun is a failed deadline observation and leaves the repair unresolved, even if the next stage yields. Canonical mutation immunity does not apply to read-only activation.

First localize the overdue interval through a bounded isolated reproduction. Reuse `RequestBudget` checks within controllable loops and waits, and existing safely deferred paths. Managed index refresh already defers its rebuild in `working_set_runtime.refresh_index()`; retain that path. Preserve current skip/truncation and budget-abstention payloads. Do not add killable threads or a watchdog framework. If the responsible operation has no safe bound, keep the repair open while independent observation and bar-loading work continues.

Canonical commits remain immune to cancellation. All existing terminal, idempotency, fencing and committed-uncertain contracts apply. Preserve exact affected paths, required component sets, derived batch custody and pending-visibility rows. This change does not choose another acknowledgement framework or bypass a fail-closed read when visibility cannot be proven.

New idempotency claims validate private runtime directories and SQLite artifacts without enumerating unrelated owner files. The private owner directory isolates those entries. Existing private lock creation, liveness checks, cleanup and legacy startup hardening remain unchanged.

### 6. Prove the delivered workflow with existing instruments

Use isolated installed before/after arms on identical inputs and reference state. Bind every run to installed bytes, runtime/embedding profile, SQLite, corpus counts, auth population, CPU cap, memory and window. Verify counts and sampled identities from input to stored rows to returned packets before deriving percentiles. Record exclusions and contention under the owning contract.

Report case-paired differences and their confidence intervals, plus CPU, RSS, failures, quality, freshness and packet/result identity separately. An interval including zero shows no measured change. Model-free CI proves its declared profile only. Runtime fixes require scoped regression checks; reuse the existing full corpus at product delivery. This artifact batch runs OpenSpec and privacy checks only.

### 7. Bound the retrieval carry by the turn and the rarity cap

Attribution put most abstaining activation time in the retrieval carry, and its cost grew with the knowledge base. Three mechanisms caused this, and each is now bounded without changing an answer:

- A word's rarity count over the admitted pages stops at `cap + RETRIEVAL_CARRY_FETCH + 1` qualifying rows. Past that window no count changes a decision, so the scan no longer reads every page that holds a common word. Past the early stop, a page whose status label cannot be classified is no longer read, so it no longer raises `ReaderViewUnavailable`; the rarity decisions are unchanged.
- Configuration decides which statistics a ranking uses (`RequestQueryContext.catalogue_ranking`, set by `working_set_runtime.request_admission`). The owner ranks on the catalogue's statistics only when `egress.release_walk_filter` returns `None`: no governance policy, collection-store marker, bound collection writer or lifecycle marker, and unrestricted owner access. That owner no longer rebuilds Okapi statistics from every admitted page after each catalogue write. A restricted reader, and the owner under any of those, filters and ranks on the admitted pages, as both rungs did for every reader before. Admission is still decided on every request.
- A corroborated ranking checks rows in rank order and stops once it holds its rows. When phrase groups are the whole test, the index names the rows that can pass first, on `+fts.rowid` so that the bm25 figures stay those of the one MATCH.

Every carry rung records a span with its admitted pages, scanned stem rows, examined candidates and a statistics-rebuild count. Every role lane records its catalogue rows, parsed pages and the time inside each read. These spans give the measurement for this decision.

**Declared behaviour change.** A request whose admission withholds nothing now ranks both rungs on the catalogue's bm25 scores, not on Okapi scores over the admitted pages. The catalogue counts raw material and pages outside the knowledge base, and FTS5 floors the IDF of a term on more than half the rows. Scores move, so such a packet can differ in five ways:

- the order of named domains, and therefore which three are carried, when more than three phrases each name one page;
- the order of the pages an abstention lists at `retrieval_named`;
- which current pages fit in a carry window crowded by retired pages, which can change whether a phrase names one page or two;
- whether a single named page clears `RETRIEVAL_CARRY_MIN_SCORE`, which moves it between carried and listed;
- which turn units the term budget keeps and which common words it drops, because the budget reads catalogue frequencies.

The ranking mode never depends on which pages exist, so adding or removing a withheld page cannot switch it. Each withholding source was checked against the catalogue the statistics read:

- Under a governance policy the owner's walk withholds by page content, with no owner-audience rule: a page with frontmatter shaped like a bridge but no approval, an unresolved scope membership, an undecodable page, or a file that vanished after the catalogue saw it. A rule for the owner therefore cannot be the test; the policy's presence is.
- A deletion or recovery marker can leave the marked path's row in the catalogue, and an unreadable lifecycle state withholds every path. Any marker, or that state, ranks on admission.
- A collection-store marker sends its owned paths, and any path it cannot place, through the full decision, which can withhold them. A marker ranks on admission.
- The access policy's `excluded` folders never enter the catalogue, and a changed access policy makes the catalogue stale until it is rebuilt. `readonly` only refuses writes. Neither affects the mode, so an owner with only an access policy keeps the catalogue's statistics.

In catalogue mode each snapshot still checks that the admission withholds no page row. The request's predicate there admits every path, so only a caller that builds a context claiming catalogue ranking with a narrower predicate can fail the check. It then ranks on the admitted pages and logs `catalogue_ranking_withheld_row`.

A request that ranks on admission, owner or restricted, ranks the phrase rung as before. Its title rung now filters and ranks on the admitted pages too, where the owner's title rung used to rank on the unfiltered catalogue and leave withheld pages to the served guard.

Some corpus-sized work remains, all of it in SQLite rather than in the carry's own loops. The request's admission reads every page row's path, the rarity window is 0.5% of the knowledge base, catalogue ranking reads every row that holds a turn word, and a write invalidates the catalogue's cached word counts.

A request that ranks on admission reuses its statistics while the complete catalogue snapshot key holds. After that key changes, it reads every current admitted path and stem string in one SQL snapshot. A framed fingerprint of these complete inputs can authorize reuse for the same scope and admitted set. Changed inputs rebuild the statistics, including SQL-only repairs that leave canonical checkpoints unchanged. The existing four-entry bound remains. This saves token splitting and aggregation after an unrelated edit, but adds hashing and temporary raw strings on cache misses.

Final cache installation replaces equivalent snapshot aliases under the cache lock.
Aliases for one admitted corpus therefore do not evict another reader's unchanged statistics.
Old snapshot readers retain their immutable statistics, but a later lookup may repeat the SQL read and hash.
The four-entry bound can retain four distinct vocabulary maps; alias compaction adds no retained state.

Under a governance policy its admission also decides every page on each request, which dominates its carry cost; this change leaves that cost as it was. The model-free gate in `tests/test_latency_gate.py` admits this growth and refuses whole-corpus work.

### 8. Release the page while optional inbound links recover

Direct page reads must not build an inbound index before releasing their held page.
The existing inbound index is the closest maintained option; its cold path currently scans every Markdown page.
Reuse that index with a non-building lookup, rather than add a second graph or a request-owned worker.
The added maintenance is current-proof acquisition and demand recovery on the existing watcher.

Every link summary uses this lookup, including local and REST calls without an MCP budget.
A complete result keeps its existing inbound and outbound lists.
Unknown inbound information uses `inbound: null`; an empty list continues to mean a complete lookup found no links.
Outbound targets come from the held caller-projected prose.
Existing page release, exclusion, byte checks and terminal disclosure remain in force.
Explicit inbound operations and mutation safety keep their complete synchronous lookup.

The lookup requires live freshness, no external-pending epoch and a matching resident index.
It copies candidates and basename evidence under the existing lock without waiting, then applies caller visibility outside the lock.
It rechecks authority before claiming completeness; missing proof, contention or change yields unknown.
It never derives freshness through a filesystem walk.
Incremental updates withdraw data and advance its generation before mutation, including updates while the cache is cold.
An update proves completeness only when retained history bridges its old checkpoint and the patch covers every changed path.
Only a current complete build or patch can publish; correctness eviction retains demand but removes data.

One existing watcher reconcile thread owns optional recovery after its required recovery and deferred work.
Runtime activation arms that work after its worker starters; required warm-up must also have ended.
Normal and quiet modes prime only requested or previously used state; performance mode can prime eagerly.
The owner yields to foreground work, observes shutdown, and rejects partial or obsolete publication.
No scheduler, registry, pool, request thread or readiness gate is added.

Recovery follows the existing mode cadence and can take one interval plus higher-priority work.
Disabled observation or continuous changes can leave inbound information unknown while page reads remain available.
The correctness cost is explicit delayed link completeness; the benefit is preserving the page response during index recovery.
No new numerical latency bar or production performance claim follows from the isolated workflow proof.

### 9. Read requests use published generations

Evidence, for tasks 3.2 and 3.3, comes from the installed service's request ledger on a large personal vault. The window is 2026-10-24 03:48 to 10:45 UTC, the boundary is MCP dispatch, and the traffic is natural agent use. `ask_memory` had n=184, p50 2,019 ms and p95 17,882 ms. `activate_context` had n=140, p50 1,289 ms and p95 9,794 ms. `read_memory` had n=113, p50 187 ms and p95 3,028 ms.

Latency grows with the other requests in flight in the same worker. The `ask_memory` p50 is 1,578 ms with no other request in flight, 4,790 ms with 1.5 to 3, and 10,848 ms with 3 to 6. Fixed-work stages slow about tenfold together: fusion goes from 54 to 589 ms, and the lexical query from 88 to 870 ms. The host had idle cores, and tools run as synchronous functions in one worker's thread pool, so CPU work in concurrent requests serializes. Other recall requests hold 78% of the overlapping time; writes hold less than 5%.

A request that runs alone still spends most of its time on work that grows with the vault, not with the request:

- The free-text artifact census walks every vault directory in 57% of calls, p50 358 ms, once per postfilter pass.
- The embedding matrix catches up in 17% of calls, p50 1,099 ms.
- The due-state block rebuilds after a key change in 8% of calls, p50 1,079 ms.
- The recall projection selects every admitted path, at up to 6 s.

The tails come from the same class: the inbound-link rebuild in context-pack neighbourhoods takes up to 112 s, and the identity census on reads up to 11 s. A request that overlaps one of these spans in another request has an `ask_memory` p50 of 8,335 ms, against 3,822 ms when it overlaps only lighter requests.

An activated server read request does no work whose cost grows with the vault. It runs no directory walk, no whole-vault scan, and no rebuild or catch-up of a whole derived structure. Each structure's publisher advances it: the write path, the watcher, the derived drain or the existing background owner. A request reads the last published generation. The freshness contract is:

- A caller sees its own committed service write on its next read. The write path updates the structure in proportion to the change, or the existing pending-visibility overlay shadows the changed paths.
- Changes from other sources, such as external edits, appear within the publisher's bound. The existing periodic reconciliation bounds missed events.
- A structure without a published generation never reads as empty. It either keeps today's synchronous path or reports incomplete coverage.
- Explicit offline callers, deployments with event-maintained indexes disabled, explicit inbound operations and mutation safety keep their synchronous paths.
- Release, revocation and access-policy decisions stay live in each request. Only the lookup structures move.

| Site | Publisher | Request reads | Own write | Stale effect |
|---|---|---|---|---|
| Free-text artifact census | Background base rebuild from event-named directories, plus a periodic walk; an add-only overlay updated by self-write registration and watcher events | Base plus overlay, with no filesystem enumeration | Overlay add before the write acknowledges | An external create that is named bare stays unresolved until its event; a path-shaped unresolved token gets one direct check; a removed name stays until a base rebuild, which withholds more but leaks nothing |
| Admitted recall path set | Catalogue publication | The set cached per admitted checkpoint, shadowed by set difference | Pending overlay | None, because the cache key is the checkpoint |
| Embedding matrix | The derived drain catches up after a batch commits | A lagging matrix with the same epoch, instance, space and policy; the single-flight catch-up starts in the background | The pending overlay shadows changed paths; the chunk proof refuses text for changed paths | Another source's new rows rank after the catch-up |
| Due-state block | Existing single-flight refresh | The newest build for the same vault, audience, resolution and purpose, after the existing re-checks | Dismissals and removals apply at once | Additions appear after the refresh; a build never crosses audiences |
| Context-pack neighbourhood | Existing inbound index and its watcher recovery | Non-building lookup; on a miss, outbound neighbours with inbound marked incomplete | The write-path patch republishes the index | Inbound neighbours are missing until recovery |
| Reference resolution | Identity projection, with owners under the knowledge-base trash folder in a table used only for references | The projection, also when stale | The write path updates the projection | A reference resolves to its last known owner until the refresh; the path decision stays live |
| Pending visibility | Proven rows kept from publication | Rows re-read only for changed bodies | Unchanged | None |

A flag that reports a lag is relative to the caller's view. A vault-wide flag such as `index_stale`, `inbound: null` or an incomplete neighbourhood flips on any write, including a write to a page the caller may not see. That gives a restricted caller a one-bit write signal, which the context-activation rule withholding `freshness_key` and `index_generation` exists to prevent. Removing the flags would instead present a partial result as complete. For a restricted caller, a lag counts only when a changed path since the published generation is one the caller may see. A lag made only of withheld paths cannot change what that caller receives, so its response is complete and stays the same. The check costs the freshness delta and one release decision per changed path. A bound on the decided paths keeps that cost fixed: a longer or unbridgeable lag reports the lag undecided. A burst of withheld writes therefore still flips the flag until the structure is published. Publishers usually keep the lag far below the bound, so the remaining signal is coarse.

The census overlay only adds names. With leftmost-longest matching, deleting a longer alias could expose a shorter withheld name inside it, so removals wait for a base rebuild that proves them.

Published admitted sets for structured eligibility and the activation carry are a separate slice. They share one new mechanism, and a stale set could leak through ranking or rarity. The slice publishes per-caller page verdicts beside the admitted recall path set. A verdict is stored only when it depends on nothing but the page, the policy and the caller. A page in a collection placement layer is decided on every request, because a store can take over a file page there without a byte change. A page served from a published verdict records no disclosure outcome of its own. A decision made only to admit a page to the carry's rarity counts or to the structured filter's candidates is not a disclosure. When the vault has a governance policy or a collection store, the release layer records an outcome for every page that a response returns; without either, an ordinary page records none, as the `disclosure-evidence` spec requires. Serving from several processes and free-threaded CPython stay out of scope: each extra reader process duplicates the in-process caches and adds an estimated 1.3 to 2.5 GB. The per-request CPU work in `accelerate-governed-recall` section 6 continues.

Proof compares the installed request ledger before and after the change, on the same concurrency buckets: alone, 1.5 to 3, and 3 to 6. Each tool needs at least 100 calls per arm, with the write count per window and a bootstrap 95% interval. Each site also has a span check: its span leaves request calls, or its p90 falls. Model-free tests guard each freshness and release guarantee above.


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

Browse `list` takes no separate threshold. It falls under the programme-wide bar, which is p95 below 1,000 ms at the caller boundary for every operation. The installed request ledger supplies the evidence at the dispatch boundary. Before the bounded listing (2026-10-18 and 19), n=61, p50 1,975 ms and p95 114,448 ms. After it (2026-10-21 to 24), n=40, p50 608 ms and p95 7,072 ms. Browse therefore fails the bar. Browse shares the contended worker, so decision 9 is its repair path. The `browse-pending` entry stays pending for a browse-specific boundary.

Read, deep, cold and background-freshness thresholds remain pending where no current applicable bar exists. Current measurements will support their recorded engineering choices. Residency selection remains conditional on the served profile's measured memory and cold/warm costs. Neither unknown grants a passing result or introduces another approval or release gate.
