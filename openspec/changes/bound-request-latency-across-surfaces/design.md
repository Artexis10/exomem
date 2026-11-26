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

A starved host showed the 50 s request budget is too long for an optional activation: on 2026-11-09 activations ran 20 to 63 s, and the slowest lost their packets. A `maps` activation can therefore run on its own budget beneath the request's (`request_budget.capped`): `EXOMEM_ACTIVATION_BUDGET_SECONDS`, set by the operator, ending at least one second before the request does. It is unset by default. Replaying the live activation log (2026-11-04 to 2026-11-10) against a 15 s deadline: of 146 calls served after 15 s, 2 would abstain (deadline before the map), about 90 would serve the map alone because the deadline lands in the turn slice's compile (mostly the conversation carry), 40 would serve the map and slice with partial leads, and 14 would serve whole. About 12% of all served calls would lose their slice and leads. The default stays unset until the carry is bounded or the turn slice keeps what it found at a deadline. When it arrives after the map is built, the activation serves the map and the leads it already found, with `task_context` partial and a `budget` reason, instead of abstaining; before the map is built it abstains as before. The `anchors` rollback keeps its abstention, and the REST and CLI doors keep their shorter limit; bounding `anchors` is a follow-up. Each activation log row records `activation_result` and `budget` so the operator can see how often the deadline limits a call. When task discovery runs after the turn slice, it compiles the slice under a deadline that leaves the task-context reserve, and the slice itself leaves one stage reserve. A caller whose discovery does not run keeps that time for the slice. `request_budget.task_context_reserve` sets that reserve with the rule of the task projection reserve: a quarter of the budget, at most 5 s. It is 1.5 s on the 6 s door, 3.75 s on 15 s and 5 s on 50 s. Without it, a slice that spent the whole budget in the retrieval carry left task discovery no time. In the same replay, about 60 of the 146 slow calls would have returned the map alone for that reason. A carry that the deadline stops is recorded as truncated, not skipped, because it had started. A carry that its boundary check refuses with time to spare used to abstain `unresolved` with no anchor, and the slice read that as `empty`. The compile now adds `{role: anchors, reason: budget}` to `missing`, skips the title listing and the related pages, and the slice reads `skipped`. The packet cache does not keep a packet with that entry, because the next call has its own clock. The call ledger keeps its `skipped` list only, so a carry that the deadline cut no longer appears in a ledger row. The activation log already keeps the whole budget block, and widening the hash-chained row is a separate decision.

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

A request that ranks on admission reuses its statistics while the complete catalogue snapshot key holds. After that key changes, it reads every current admitted path with its page content hash in one SQL snapshot, and reads stem strings only for pages whose hash is new, changed or absent. The stem string is derived from the page bytes, the path, the knowledge base folder name and the catalogue identity, which includes the tokenizer version. Only the page insert writes it. A rebuild's reconcile route can also copy the whole catalogue, but only under the same identity and release, and the copy keeps each page's hash and stem string together. So a framed fingerprint of those inputs can authorize reuse for the same scope and admitted set. Changed pages rebuild the statistics. This reuse check does not notice an out-of-band SQL edit of the stem text that leaves the page hash unchanged. The reconcile route keeps that edit too, because it keeps every page whose bytes, modification time, scope and semantic generation are unchanged. Only a rebuild that parses every page, or a new write of that page, rewrites the stem text. A restart rereads the stored text: the statistics then agree with the index, but the edit stays. The existing four-entry bound remains. This saves token splitting and aggregation after an unrelated edit, but adds hashing and temporary raw strings on cache misses.

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
| Review queues | One pass of the review audit and one activation scan per vault, which a background worker runs and holds in process memory for an unrestricted caller | Copies of the held findings, which the request filters, ranks and annotates as before | A due item that the projection drops leaves the listing at once; the items of a deleted or moved page leave it; the items of an edited page carry `page_changed` | A new finding appears after the next pass |

The review queues hold findings in memory only, so a restart starts empty. Only the background worker of the serving runtime runs a pass. A request never runs one: it reads the held pass, or it runs its own audit of its own categories, as before. So no request does more work than before. One pass runs every review category, and costs more than the audit of one queue. A held pass is current while the vault's change generation, the UTC day and a maximum age (30 minutes) hold. The listing reply names the pass time in `as_of` and says in `refreshing` whether a pass is due or running.

- An unrestricted caller is a bound caller from whom the release rules can withhold no page. The service holds a pass only for an unrestricted caller, and only such a caller reads it. A restricted caller, a bound collection writer and a call with no principal run their own pass in the request, as before. A release verdict can depend on live state that no key names, so the service holds no pass for them.
- A read with no held pass keeps the request path and asks the worker for a pass. A stale read answers from the held pass and asks the worker for one refresh. The worker runs a pass when a read asks for one. It also runs the first pass of the process once the service has been quiet for 60 seconds. Nothing else starts a pass: a pass that nobody reads is not renewed, and a failed pass is not tried again until a read asks. Two passes start at least three times the last pass's duration apart. That gap doubles with each failure in a row, up to 64 times, and a failed pass counts as at least one second long. A failed pass replaces nothing, and a pass that runs past 30 minutes is abandoned. After a failed pass, a read of a stale pass keeps the request path until a pass succeeds, so a failing check cannot leave a caller with an old listing.
- A decision on an item whose page changed after the pass is refused with `REVIEW_ITEM_CHANGED`, as a changed fingerprint is. A decision on the item of a moved page is refused with `ITEM_NOT_FOUND` until the next pass.
- A stale answer keeps the due items when the due-state projection cannot be read.
- A process with no worker (a stdio server, the CLI, a standby) and a vault without a live change generation hold nothing.
- A decision binds an id from whole queue reads. These reads use the held pass and record no first-surfaced stamp.
- The held pass is not a release decision. Release, review state and ranking stay live in each request.
- Inputs other than Markdown pages, such as registries and governance files, do not move the change generation. The maximum age bounds how long a held pass can miss them.

A flag that reports a lag is relative to the caller's view. A vault-wide flag such as `index_stale`, `inbound: null` or an incomplete neighbourhood flips on any write, including a write to a page the caller may not see. That gives a restricted caller a one-bit write signal, which the context-activation rule withholding `freshness_key` and `index_generation` exists to prevent. Removing the flags would instead present a partial result as complete. For a restricted caller, a lag counts only when a changed path since the published generation is one the caller may see. A lag made only of withheld paths cannot change what that caller receives, so its response is complete and stays the same. The check costs the freshness delta and one release decision per changed path. A bound on the decided paths keeps that cost fixed: a longer or unbridgeable lag reports the lag undecided. A burst of withheld writes therefore still flips the flag until the structure is published. Publishers usually keep the lag far below the bound, so the remaining signal is coarse.

Catalogue coverage uses the checkpoint of its retained SQL selection, including when another request publishes a newer catalogue.
Both initial admission and finalization apply the existing caller-relative lag proof.
The owner keeps the vault-wide flag, and invalid selected evidence always reports incomplete coverage.

For deleted pages, the selection retains references to existing verdict maps that match its exact SQL generation and admission identity.
The maps remain immutable, so another request can replace the shared cache entry without changing this selection's proof.
Finalization builds the current release predicate outside the cached policy snapshot and checks its identity before using those retained verdicts.

An activation can cross a catalogue publication between its initial lexical pass and compilation.
Compilation may use its captured SQL checkpoint when both checkpoints bridge to the current state through changes withheld from the caller.
Both proofs use current authority, including purpose and grants, and remain subject to the existing decision bound.
Compilation owns that checkpoint and its exact verdicts; earlier selections keep their original checkpoints and proofs.
Finalization repeats both lag proofs and preserves the existing selected-source checks.
A visible change or missing proof keeps the existing incomplete or unavailable outcome.

#### Graph publications remain coherent during repair

An old graph checkpoint describes repair lineage after partial drains, not necessarily the current rows.
Restricted graph reads therefore need a coherent published view while maintenance keeps partial progress.
Each request keeps its own SQLite connection, transaction, progress handler and deadline.
Short reader proofs use one process-local mutex before the existing canonical boundary probe.
A reader may wait for another reader's proof within its deadline; it never waits for a canonical writer.
Both locks release before queries, hydration and release decisions.
The mutex survives publication changes, while a retired lineage invalidates its pending readers.
A canonical holder reenters directly, so it never queues behind a reader that probes that holder.
This introduces no writer fairness guarantee or shared query connection.

Integrity purge rotates the existing graph incarnation and withdraws availability in its transaction.
Admission and finalization compare the captured incarnation through a fresh connection under a short canonical hold.
This check also covers the strict fallback without retained process state.
An intervening recovery cannot make the purged incarnation valid again.
Ordinary incremental publication keeps the incarnation, so withheld repair can preserve an open read.

Retain the first predecessor of each changed row in five saved tables:
`graph_saved_nodes`, `graph_saved_edges`, `graph_saved_parent_refs`, `graph_saved_dependencies` and `graph_saved_dependency_coverage`.
`graph_publication_meta` holds the metadata of the complete publication that those predecessors preserve.
Derive saved columns and primary keys from the existing schema, and retain each predecessor's explicit `rowid` for authoring order.
An absence marker hides a row created after publication; it carries the primary key without a predecessor row.

SQLite triggers capture each row before insertion, replacement, update or deletion, in the transaction that changes it.
Preserve the first image with a guarded `INSERT ... SELECT ... WHERE NOT EXISTS`.
An outer `REPLACE` must not replace that image through its conflict policy.
Remove an absence marker when its row no longer exists, and restore it after replacement when necessary.
This must hold with recursive triggers enabled or disabled.
Repeated create-delete cycles must not accumulate marker history.
Incoming-edge deletion captures the affected edges, without copying their authors' unrelated rows.

Connection-local views combine unchanged main rows with their saved predecessors and exclude new rows marked absent.
Maintenance keeps using the main tables.
A complete publication updates its metadata and acknowledgement and clears saved rows in the same transaction.
Integrity repair withdraws invalid publication proof instead of retaining suspect rows.
Schema migration initializes this state through the existing complete rebuild; it never captions mixed rows as a complete publication.

A warm worker pairs the publication with an immutable existing resolver and its topology fingerprint.
Body-only publications reuse that resolver when its topology and policy remain unchanged.
A worker without the matching resolver keeps the existing unavailable outcome until recovery supplies it.
There is no second persisted resolver or resolution algorithm.

Row coherence does not prove that a local watcher has observed every committed write.
Governed publication associates the canonical graph generation and digest with its known local recall checkpoint.
Only contiguous known canonical generations advance that association.
A complete corpus callback can prove one paths-scoped epoch without a trusted callback origin.
Short canonical holds surround corpus publication; the corpus work runs outside those holds.
The next hold compares the exact prior association, durable floor and checkpoint, and current policy.
The consumer delta must name every epoch endpoint with its exact changed or deleted classification.
Current guarded bytes and target signatures must match that delta and the epoch.
Byte proof runs after the outermost canonical holder releases both mutation guards, on the same thread.
Pending callbacks retain each unresolved endpoint's latest digest and signature within the existing 16-path bound.
Readers and incremental publication cannot use a pending association.
A final short hold compares the exact pending candidate, epoch, local checkpoints, file identities and signatures.
The release hook coalesces callbacks by key and clears its queue before running them.
Callback failure withdraws optional proof without changing the committed mutation's result.
A subset does not suffice: a changed endpoint can retain its previous stat signature.
Full checkpoints, gaps and incomplete histories require the existing full-source recovery.
Duplicate callbacks cannot attach a newer local checkpoint to the same canonical epoch.
The canonical epoch must cover the whole resolver source domain, including Markdown outside the KB.
This coverage includes creation, body changes, deletion, restoration and both endpoints of a rename.
A rename stages its floor before moving bytes and publishes its exact endpoint checkpoint before a later rewrite batch.
The destination digest describes the bytes installed by the rename, before any content transformation.
The move queues that checkpoint without starting acknowledgement during its rollback window.
The existing mutation boundary covers the compound move and precedes the semantic creation lock.
Direct callers retain the existing repair handoff after that boundary releases.
Only proved child rollback and successful inverse renames permit restoration of the earlier epoch.
Committed, partial or unknown outcomes retain their epoch or floor and existing full repair custody.
A complete callback origin cannot explain an earlier foreign commit that the epoch did not record.
A process counter whose proof lasts only one invocation cannot attest a retained publication.
When epoch coverage is incomplete, only the existing complete-source proof may establish a new baseline.
Admission and finalization check the durable checkpoint and generation floor under the mutation coordinator.
A foreign advance without a complete local bridge keeps the existing unavailable outcome.
Both boundaries retain the caller-relative lag proof, current authority and selected-source checks.
The existing 16-path bound and unknown-deletion behavior remain unchanged without an exact source-bound verdict.
Only successful source-proved replacement or sealed republication establishes a new baseline and detached resolver pair.
An incremental publication advances an existing association after its final checks; a notification alone proves nothing.

The supported Python SQLite interface does not expose historic snapshots; its [backup API](https://docs.python.org/3.12/library/sqlite3.html#sqlite3.Connection.backup) copies the whole database.
The saved rows add custom versioning and trigger maintenance, but their work follows actual row changes.
The proof must cover repeated replacements, high-fan-in deletion, independent readers, marker retirement and foreign writes before watcher delivery.

For a deleted path, missing verdicts, changed authority or unbridgeable history leave the lag visible.
A failed optional metadata read discards that retention attempt; selected rows still serve.
The selection creates no historical verdict and copies no corpus map.

Graph deletion reuse also requires the catalogue view's vault checkpoint to equal the retained publication's recall checkpoint.
Each verdict uses its own scope checkpoint and exact SQL generation under fresh caller authority.
The verdict must explicitly withhold the same full source hash read from that publication's connection-local file row.
A matching vault checkpoint alone proves nothing when a KB-only catalogue update replaced the shared source row.
The graph keeps its consumer delta, canonical bridge, incarnation checks and independent deadline.

The inbound projection replaces its source membership set with full hashes computed from text its builder or patcher already reads.
Those records authorize deletion proof only while associated with a proven complete inbound checkpoint.
Applying a partial patch invalidates that association; proven publication restores it.
A request copies only the bounded deleted-path hashes under the existing index lock and checks the same cache generation afterward.
It never copies the source map, builds the projection or reads deleted source bytes on the request thread.

Both consumers reuse existing immutable catalogue verdict maps and retain no historical decision store.
An existing request can hold its exact map through publication; a later request cannot recover a retired map.
The request pins proved deleted paths before constructing its consuming reader.
Local scopes reuse an outer request's exclusions, and missing scope custody leaves the lag visible.
Fresh readers preserve the same principal and purpose outside cached policy snapshots at admission and finalization.
Graph text and edges, inbound source contexts and basename evidence all exclude the pinned paths.
The owner, changed sources, revoked authority, missing hashes and unpublished inbound associations keep their existing lag outcomes.

The census overlay only adds names. With leftmost-longest matching, deleting a longer alias could expose a shorter withheld name inside it, so removals wait for a base rebuild that proves them.

Published admitted sets for structured eligibility and the activation carry are a separate slice. They share one new mechanism, and a stale set could leak through ranking or rarity. The slice publishes per-caller page verdicts beside the admitted recall path set. A verdict is stored only when it depends on nothing but the page, the policy and the caller. A page in a collection placement layer is decided on every request, because a store can take over a file page there without a byte change. A page served from a published verdict records no disclosure outcome of its own. A decision made only to admit a page to the carry's rarity counts or to the structured filter's candidates is not a disclosure. When the vault has a governance policy or a collection store, the release layer records an outcome for every page that a response returns; without either, an ordinary page records none, as the `disclosure-evidence` spec requires. Serving from several processes and free-threaded CPython stay out of scope: each extra reader process duplicates the in-process caches and adds an estimated 1.3 to 2.5 GB. The per-request CPU work in `accelerate-governed-recall` section 6 continues.

Proof compares the installed request ledger before and after the change, on the same concurrency buckets: alone, 1.5 to 3, and 3 to 6. Each tool needs at least 100 calls per arm, with the write count per window and a bootstrap 95% interval. Each site also has a span check: its span leaves request calls, or its p90 falls. Model-free tests guard each freshness and release guarantee above.


### 10. Background passes give way to requests

The service runs its request threads and its background threads in one process, so they share one interpreter lock. A background pass that runs beside a request takes CPU time from it. Recalls that overlapped background passes ran 1.4 to 2.2 times slower than recalls that did not. The operator measured this on the installed service; this change does not repeat it.

`foreground_priority` already holds the mechanism: a pass runs inside `bulk()` and waits for requests in flight at each `yield_to_foreground()`, within a progress floor. This decision applies it to the remaining passes and adds no mechanism. The rule for every site: the pass only waits. It skips, reorders and drops no work, and its cancellation and budgets work as before.

A yield never parks a thread that holds a lock, mutation boundary, single-flight build or connection that a request can wait on. A site with no safe point is left unchanged.

| Pass (thread) | Unit that yields | Held at the yield | Left unchanged |
|---|---|---|---|
| Working-set warm (`exomem-working-set-warm`) | Each page of the page walk and each fold of the context catalogue | Nothing; the index write starts after the walks | The index write itself |
| Graph drain (`exomem-graph-drain`) | Each drain pass, and each single-receipt replay | Nothing; each pass is its own `bulk()` scope, because an idle wait inside one long scope would earn wait allowance it never used | `drain_paths`, which runs under the mutation boundary |
| Vocabulary recovery (`exomem-vocabulary-recovery`) | Each job | Nothing; the page of jobs is a closed snapshot and no claim is open | Shutdown still ends the loop between jobs |
| Freshness reconcile (`kb-freshness-reconcile`) | Each entry of the stat walk, and one wait before the deferred drain and before each lexical warm | The replacement lock, which its waiters wait on inside `awaiting_bulk` | The startup seed, which gates admission; the delta dispatch and the media reconcile, which a yield would stretch |
| Watcher flush (`kb-file-watcher`) | One wait before each batch | Nothing | The batch itself, which withdraws and republishes availability; the startup catch-up and final flushes |
| Identity catalogue warm (`exomem-identity-catalogue-warm`) | Each directory entry of the optimistic walk | Nothing; followers of the single flight wait inside `awaiting_bulk` | The locked fail-safe scan, which holds a boundary |
| Matrix load (`exomem-matrix-load`) | Every 512 rows of the read that runs off the index lock | The read snapshot only; a caller that holds the index lock never yields | The load a caller runs inline under the lock |
| Review pass (`exomem-review-pass`) | One wait before the pass reads anything; each check of the audit; each page of the audit's page walk, of the artifact role state build, of the unprocessed-source, relation-debt, collection-candidate, note-cohesion and name-drift checks, of the entity recurrence sweep over parsed pages and of the activation scan; each block of products of the near-duplicate pair search | Nothing a request waits on. A store-mode page walk holds a pooled read snapshot, which only delays a checkpoint; the ownership proof holds its guard while it resolves, not across the audit; the usage-log read scope is a context value with no lock | `artifact_role_state.inspect`, which the served read runs with its own read connection open; `entity_recurrence.collect_evidence`, which the persisted sweep runs with a store connection open; the work inside one unit |

The derived drain takes the boundary in turns and ends a turn between units. A turn now also ends when a request is in flight (`foreground_elsewhere()`), not only when a writer waits. The drain then waits for the request outside the boundary, unless the turn is a loan from a long mutation, whose lender is itself foreground work.

The share of its elapsed time that a pass may wait (`MAX_WAIT_SHARE`, 0.5) is a setting, `EXOMEM_BULK_WAIT_SHARE`, read from the process environment at each call. Unset, malformed or out-of-range values keep 0.5. A pass that sets its own `PassBudget` keeps it.

The new yields also fire inside bulk scopes that already existed, because the walks and loops they sit in are shared: governed maintenance jobs (`background_job()`), the derived-drain dispatch, the whole-vault graph pass and the graph proofs of the scheduled catalogue fold. In each, a yield only waits, within that scope's own progress floor. The identity walk is also the locked fail-safe scan's walk. That scan holds a boundary, so its yields never wait, but it now has cancellation points that it had none of.

The review pass's units are shared too. The audit's checks, the artifact role state build, the name-drift scan, the entity recurrence sweep over parsed pages and the activation scan also run in the due-state projection rebuild and in the maintenance reconcile. A yield there waits only when the caller runs in a bulk scope and holds no mutation boundary; a request runs in no bulk scope, so its yields do nothing.

The review pass waits at two grains. The units about `MAX_BULK_WORK_SECONDS` (0.05 s) apart are the pages of each walk and the blocks of the pair search. The coarser units are each check and the setup that a check does before its first page. Measured on a synthetic vault of 1,400 notes and 200 sources, with no request in flight: one audit and one scan made 15,727 yields. The longest stretches between two yields were 0.26 s (the entity recurrence setup before its first page), 0.24 s and 0.22 s (a registry read on the first page of a walk), and every other stretch was 0.08 s or less. The findings were the same as before the change (604 and 1,218). A pass that waits behind a request is also the pass's own limit: the limit's timer sets the pass's `StopEvent`, so the limit ends a parked pass at once, as a stop does. The pass's span carries `waited_ms`, as the other passes' spans do.

A leader's stop is its own. A stoppable pass can lead the single-flight identity build, and its stop then ends only its own pass. The flight records the stop as cancelled, not as an error: a follower never re-raises it, because it is a `BaseException` that its caller may not handle and that would end the caller's own pass. A follower with no request budget (a background caller) claims the build itself, or follows the next leader, and gets the inventory it got before the walk had cancellation points. A follower on a request budget never walks past its deadline: it follows the next leader, or takes the typed unavailable outcome that it takes when its wait runs out. The matrix-load join and the freshness replacement-lock wait carry no outcome from the thread they wait on, so they cannot broadcast one.

A parked yield ends on its scope's stop. The stop is a `foreground_priority.StopEvent`, whose `set()` notifies the one condition that parked yields wait on, so no thread polls and no stop site needs a second call. The graph drain, derived drain, file watcher, maintenance jobs, original fixity and the server runtime's vocabulary and shutdown stops are `StopEvent`s. The working-set builds stop on the server runtime's shutdown event. A scope whose stop is a plain `threading.Event` still ends at the wait's cap, and a scope that carries no stop keeps the plain condition wait.

The spans and the log line that time a background pass count its waits for requests as work: `embeddings.matrix_load.rows`, `reserved.identity.baseline_build`, the background `catalogue.fold` and the matrix background-load log line. Each carries `waited_ms`, the wait inside it, so a later comparison separates work from waiting.

Risks that remain open.
- A longer identity walk can see more token mismatches and fall back to the locked scan more often.
- The matrix load keeps its read snapshot open while it waits, which delays a WAL checkpoint. A stretched off-lock read also gives writers more time to outrun it, so `_full_load_locked` becomes more likely.
- Warm windows get longer under traffic: a warm that waits for requests stays unwarm for longer, and so do the readers that join it.
- `graph_drain.seconds_until_pass()` ignores the new initial yield, so it can report a pass as due while that pass is still waiting for a request.
- A review pass that waits for requests runs longer, so a held pass is older when it ends, and the 1800 s limit and the gap before the next pass count the waits.
- The audit runs in the collection ownership read scope, which proves the binding again when the scope ends. A longer pass gives an ownership transition more time to land inside it, and then the pass fails as unavailable and is tried again after its gap, as any failed pass is.

Proof: one model-free test per pass shows no progress while a request is in flight and the same result after; the installed comparison belongs to task 3.16.

### 11. Corpus statistics stay exact under incremental order maintenance

A write moves the snapshot generation, so the next recall rebuilds its BM25 figures for each admitted page set and for the unit corpus. A rebuild re-tokenized only the changed pages, but then walked every term of every page to restore the first-seen order of the words, and the unit figures counted the whole unit corpus again. The figures must equal `rank_bm25` 0.2.2 to the last bit: the idf floor sums over the words in first-seen order, scanning the documents sorted by key.

A base now also keeps the introducer of each word: the first document, in key order, that holds it. It also keeps the words that each document introduces, in that document's term order. One helper updates both for a change set, and both builders call it:

- A word that an entered document holds moves there if that document comes before its introducer.
- A word whose introducer left or dropped it moves to the next document that holds it. One forward pass from the earliest such introducer finds all of them.
- A word that no document holds any more leaves the counts.

The per-document lists read in key order are the order `CorpusStatistics.from_counts` sums in. A rebuild usually costs the changed documents plus one pass over the vocabulary. A word whose introducer left or dropped it sends one forward pass over the documents after that introducer, which can walk most of the corpus. The independent review of this change measured that worst case on a synthetic corpus of the vault's size: 188 ms, against 406 ms for a full count. The unit corpus keeps each ref's winning tokens, so only the refs whose winner changed are counted again. `CorpusStatistics.of` stays the single definition, and seeded edit sequences compare every field with it bit for bit.

A rebuild still paid for the whole vocabulary and the whole unit corpus. Writing the frequency mapping again in first-seen order, and two logarithms per word in `from_counts`, cost 63 to 84 ms and 14 ms a build on a 15,600-page corpus of about 232,000 words. The unit figures merged every parent's units again: about 62,000 units for one changed parent. Now:

- Each introducer keeps the counts of the words it introduces, in their order. A change copies the frequency mapping and patches the words whose counts moved. It writes again the ordered counts of the introducers whose words moved and patches the other moved counts in place.
- The idf floor's sum reads those counts in key order. It computes `bm25.idf_term`, the one definition of a word's idf, once for each distinct count, and adds the terms from 0, left to right. That is the same sequence of the same floats, so the floor stays bit-exact. The frequency mapping keeps no order of its own and is only looked up. A build costs 13 to 21 ms, whether it edits or adds a page.
- The scope's unit base keeps the merge its corpus came from: the admitted set, the interpretations' dependency, each parent's revision and units, and each ref's winning tokens, first occurrence and occurrences. A later build of the same admitted set merges again only the parents whose revision moved, and counts only their refs. A build in which a parent that only its caller may interpret emits a unit merges every parent. A build whose such parents emit no unit still patches, and keeps none of them in the base. The merge's corpus and occurrence mappings are its statistics entry's own. Its revisions and parents are its own: about 12.5 MiB at 62,400 units on synthetic data, which `_UNIT_BASE_BYTES_MAX` does not count. One merge per scope bounds them.
- The stemmed text of a changed page is read by its key. Led by the pages of the scope, the read walked every page: 19 ms for one page of a 15,600-page catalogue.

`CorpusStatistics.of` shares `from_counts` with the incremental builds, so a test compares the floor with `rank_bm25` itself, and the seeded unit edits compare every merge with a store that kept nothing. On the same corpus with about 62,000 units, an activation after a write cost 974 ms of request-thread CPU before this change and 751 ms after, the median of four rounds each; one with no write since the previous cost 377 and 381 ms. The page path, hash and digest reads, the recall rows and the unit revisions still read the whole scope after a write: the page reads about 80 ms of the remaining 370 ms, the recall rows about 80 ms, and the unit revisions read about 17 ms.

With no base, or past `_ADMITTED_BASE_TERMS_MAX`, `_ADMITTED_BASE_WORDS_MAX` or `_UNIT_BASE_BYTES_MAX`, a build counts every document in order, as before. A corpus that holds an owner-only parent's units never enters the shared unit base.

The page bound was 4 million unique-term references, and a vault-sized corpus sits near it: a 17,000-page personal vault held 3.7 million in November 2026. Past the bound every rebuild after a write tokenizes every page again. On a synthetic 15,600-page corpus of 4.5 million references with about 62,000 units, an activation after a write cost 2,590 ms of CPU past the bound and 933 ms with the base kept. An activation with no write since the previous one cost 269 ms past the bound and 354 ms with the base kept: a kept base also makes the postings count query words from page terms instead of full-text matches. The change gains while more than about one recall in twenty follows a write: it saves about 1,657 ms after a write and adds about 85 ms without one. The bound is now 12 million references. On synthetic Zipf corpora a base holds about 88 MiB at 3.4 million references and 196 MiB at 10.6 million. The live vault's KB scope held 244,000 distinct stemmed words at its 3.7 million references; its vault scope's count was not measured. Introducer maps grow with the vocabulary, so a corpus past 1 million words (`_ADMITTED_BASE_WORDS_MAX`) keeps no base, whatever its references. That includes a corpus under 4 million references, which kept a base before. The worst case under both bounds, a uniform vocabulary of a million words, holds about 290 MiB for one base and its statistics entry, and about 540 MiB for four bases of one scope, which share page entries; bases of two scopes share none.

### 12. A catch-up that writes outran stays a catch-up

The repair worker answers a lagging catalogue with a lag-only catch-up and runs the whole-corpus pass only when the catalogue then fails its exact-current proof. A write that lands between the catch-up's last read and that proof fails it too: the projection moved, but the catalogue is not wrong. An installed service on 2026-11-22 ran five whole-corpus passes in eight hours with about 330 writes. Three of them followed a catch-up whose proof then failed while writes held the mutation lock. A request-path repair request with no cause started the fourth, and the fifth was the warm after a promotion. They ran 233 to 1,773 s each, and readiness reported lagging in 58 of 120 samples over one hour.

When the proof fails after a successful catch-up, the worker re-reads the lag the same way the catch-up does. A lag that is not empty means a write landed after the catch-up, so the worker queues another lag-only catch-up and logs the result `advanced`. No lag means the catalogue is wrong at the live checkpoint, and the whole-corpus pass runs as before. Any refusal of the re-read (schema, identity, instance, incomplete history, source proof) also keeps the pass and logs its own reason. Each re-queue needs a new write, so the chain ends when writes stop. A lagging catalogue stays reported as lagging, and only the strict proof admits it.

Proof: `tests/test_lexical_deferred_upsert.py` injects a save between the catch-up and its proof and checks that no whole-corpus pass runs and that the catalogue ends exactly current. A sibling test refuses the proof with nothing lagging and checks that the pass still runs. The installed comparison belongs to task 3.24.

### 13. A search query does not queue behind the shared model turn

Every model call takes one process-wide execution turn. A bulk encode takes it one batch at a time, and a model load takes it for the whole load. Page search encoded its text query and its image query on that turn, so each waited out whatever held it. On an installed service on 2026-11-22, between 19:00 and 21:30 UTC, the text query waited p50 1.46 s, p90 3.8 s and at most 9.8 s for its turn, for an encode of about 0.1 s. Query preparation outside the text encode, mostly the image query, took p50 2.1 s more. One search, a minute after a worker promotion, waited 31.4 s while the warm-up loaded the reranker. The reranker loader held the turn across its tokenizer guard and its framework import, and it took the turn even to return a resident model.

The search query encodes of page search, find and passage search now go through one helper, `embeddings.encode_query`, which marks the encode as a search query's (`query_turn`) and selects the index's encoder space. Page search's prepared and fallback sites, find's own fallbacks and passage search all call it, and page search's image query runs under the same mark. The governed projection search encodes its query against its own projected index and still takes the shared turn. The mark holds no turn. Each batch takes its turn inside the encode, so the wait is measured as `execution_wait_us`, and the turn follows the encoder that actually runs. On an encoder that runs inferences side by side (an ONNX Runtime session), the batch takes the query lane and waits only for other queries. The image query tower is such an encoder: its tokenizer is configured once at load. The lane orders turns only: every query encode, text or image, still holds a process model admission, so the process bound on model work is unchanged. Any other encoder takes the shared turn, as before. A previous encoder during a re-embed keeps its own gate, so a query on the serving sidecar still never waits behind a build batch. The reranker and image query loaders return a resident model without the turn. The reranker loader runs its guard and imports before the turn, so the turn covers only the load, as in `get_model`.

Proof: `tests/test_passage_evidence.py` holds the shared turn with a capture inference and checks that a page search and the managed query preparation on a concurrent encoder still answer. `tests/test_command_path_spans.py` checks that a query waiting for its lane reports the wait as `execution_wait_us`. `tests/test_embedding_migration.py` holds the shared turn with a build batch and checks that a query on the serving sidecar still answers. Two tests in `tests/test_model_preload_offline.py` check that a query takes the turn while a cold reranker runs its guard and its import, and that a resident reranker returns while a batch holds the turn. `tests/test_clip_query.py` holds the turn with a batch and checks that page search's image query preparation still answers. Each fails without its part of the change. The installed comparison belongs to task 3.25.

### 14. Recall rereads only the rows that a write changed

After a write, the next recall read every page row of the catalogue three times. It read the recall rows (path, title, emitted parent, scope flags and raw-material flag) for both scopes. It read the content hash and full-text row of each admitted page, to derive the page identities and their fingerprint. It read the unit revision of each admitted parent. On the synthetic 15,600-page corpus of decision 11, these reads cost about 80, 80 and 17 ms of the 370 ms of request-thread CPU that a write added to the next activation.

The catalogue now keeps a change journal, `catalogue_changes(seq, path, stamp)`:

- Triggers on `pages`, `unit_revisions` and the full-text content table write one row for each row that a write adds, changes or removes. The row names the page path and carries a random stamp. The triggers run in the writer's transaction, so a snapshot holds exactly the journal rows of the writes that it sees.
- A trigger keeps the newest 16,384 rows. Rewriting a page journals about 9 rows plus 4 for each unit row that it replaces.
- `create_catalogue_schema` adds the table and its triggers in place, so a file-mode catalogue needs no rebuild and no schema version change. A store-mode vault migrated after this change gets them through the same DDL, and its comparison with a fresh derivation leaves the journal out, as it leaves out `meta`. A store migrated before the journal existed gets it at its next derivation. Each drain transaction creates whatever part of the journal is missing (`rederive.adopt_change_journal`) and records every journal object in `store_mode_objects`, so a rollback drops them all. When the creation fails, the drain undoes it and goes on: a missing journal only makes reads slower, while a failed drain would leave every later write underived. An empty journal is correct the moment it exists, so no page is derived again and no owner pass runs. Store mode refuses Source erasure today; an erasure there must restart the store's journal, as file mode does.

A store keeps the page rows, page identities and unit revisions of its two latest snapshots (`_CatalogueRows`). A kept state answers again only at the same generation and journal position: a revision-only write moves the position, not the generation. A journaled write that changes no field the state reads, such as a change of modification time alone, also moves the position, and the next request then patches those paths, under 1 ms. For a new snapshot the store patches a kept state when the journal vouches for the gap, and reads only the rows of the paths written since, each by its key. The journal's bound caps the gap, so a patch reads at most 16,384 paths. Otherwise it reads every row, as before. The journal vouches for a gap only when all of these hold:

- Every journal trigger exists, for each journaled table that the catalogue has. One mapping names the journaled tables for the DDL and for this check.
- `PRAGMA schema_version` is unchanged. DDL can drop a trigger or replace a table without a journal row.
- The journal still holds the kept state's newest row, under the same stamp. A catalogue built or replaced beside the live one, a copy that diverged before that row, or the bound leaves another row there or none. A copy taken after that row holds the same rows up to its copy point, so its gap still names every path that moved.

The journal names a page's path after the page's rows are gone, so a Source erasure restarts it once the derived owners drop the erased page. The restart writes one row that names no page and deletes every older row, in one transaction. The journal then names no erased path, and no position from before the restart can vouch for a gap, so each kept state reads whole once. Deleting only the erased page's rows would let a kept state patch across a gap that no longer names that page.

A repair rebuild must not bring those rows back. It copies the live catalogue or parses the files, and replays the writes that land while it builds, so it can still name the erased page. Three rules close that:

- The rebuild reads the Source retirement generation when it starts, and declines to publish when an erasure advanced it since, as a detached catalogue's adoption does. It declines before it builds when it cannot read the generation.
- A rebuild that copies the live catalogue starts its own journal, so it never carries history that the live journal has since dropped.
- The eraser advances the generation once, before it restarts the journal. A rebuild that publishes before the restart is restarted with the live catalogue. One that publishes after it either started before the advance and declines, or started after the page's rows were dropped. A caller that repeats a refused restart does not advance again, because each advance makes the repair rebuild that the refusal schedules decline.

The delete triggers stamp a page a unit revision even when its rows go, and that revision names the page. Every delete now drops it in the two primitives that remove a page's rows or its units, so no delete path keeps it: not the live delete, not the replay, and not the reconcile that drops a copied page whose file is gone.

The recall rows, the page identities and their fingerprint, and the unit revisions and their fingerprint are now derived from that state. They hold the same values in the same order as before, so every cache key and every result is unchanged. Each state fill records the span `lexical.catalogue_rows`, with `patched` and `paths_read`. SQLite's session extension records changesets, but Python's `sqlite3` module does not expose it. `sqlite3_update_hook` sees only the writes of its own connection.

Proof: `tests/test_catalogue_change_journal.py` checks that the journal names every path that a page, full-text or unit revision write touched. It also checks that a diverged copy, DDL, a missing trigger, the bound and a restart each stop a patch. The Source eraser's test that no live store names an erased page covers the restart's purpose. A seeded run of edits, renames, deletions and moves in and out of the knowledge base and its raw material patches at every step. At each step it equals a store that kept nothing, row for row and in order. The existing seeded tests of the page and unit statistics now run through the patched state and still equal a store that kept nothing.

Measured on the synthetic 15,600-page corpus of decision 11, two runs an arm, eight rounds a run:

- A write added 550 and 664 ms of request-thread CPU (p50) to the next activation before the change, and 291 and 353 ms after it.
- The page identities took 15,621 `_page_identity` calls (14 to 16 ms) before, and one call after. The recall rows took 35 to 71 ms before, and 21 ms after.
- The page statistics' own work fell from 170 to 196 ms to 109 to 129 ms. A patched state fill took 2 ms.
- One whole state held 10 MiB, with a 14 MiB peak while it filled. A patched state shares every row that did not change, so the second kept state adds only the rows that a write changed.
- On a generated 15,600-page catalogue, with equal fingerprints, a whole fill and the three readers on it took 4.5 to 24 ms more (p50) than the three reads before the change: 76.6 against 66.6 ms with garbage collection on, 66.9 against 62.4 ms with it off, and a median paired gap of 24 ms when the arms alternate (the pull request's review reran the bench scripts). The difference is the framing that each row keeps, which makes every patched fingerprint a single hash. A whole fill happens after a start, a catalogue replaced or changed by DDL, or a gap that the journal cannot vouch for.
- Rewriting a page with four units journals 25 rows. The write time per page showed no measurable change: −1.8 to +1.0 ms on 15.6 to 21.0 ms.

The second run of each arm shared the host with other work, and in three of the four runs a write's rebuild sometimes reached the following activation. So the activation medians are noisy. The function times show where the saving lies.

### 15. A write that used its call's time does not start its commit

A write prepares before it commits: it resolves its page and builds the corpus context that its checks read. On a starved serving host on 2026-11-25, preparation once took 13 minutes, and refusals took up to 240 s, while clients give up after 60 s (an agent harness) or 240 s (a web client). The write then landed, or failed, with nobody listening. The operator chose, on 2026-11-26, to stop such a write before its commit, and then limited the stop to saves the write intake can queue.

For a save the write intake can queue (`write_intake.queues_busy_refusal`) and the server can safely run again, the call's first write guard (`writer_lease.mutation_guard`) now refuses before it acquires the vault mutation boundary when the call's own deadline has passed (`request_budget.commit_may_start`). The refusal is `MUTATION_BUSY` with `committed: false` and `cause: request_budget`, so the intake queues the save and its drainer applies it. Any other write still commits late, as before: a refusal could not queue it, so a write whose preparation always outlives the budget on a loaded host would otherwise never land. A capture that fetched a caller's temporary file links also commits late (the write intake does not admit it, under the writer lease's `rerun_safe` rule), because a replay would fetch them again or find them expired. Nothing is stopped mid-way: preparation runs to the guard as before, and once a call has taken the boundary, every later guard of the same call runs, because it belongs to a commit already under way. The read-only `consistency_guard`, holds outside an intake save and the intake's replay, which runs with no call budget, are never refused. A wait for the boundary cannot help this refusal, so the waits that absorb an ordinary busy boundary pass it on at once (`mutation_lock.waits_out`).

The gate binds no read checkpoint over preparation. Several readers degrade quietly under a checkpoint (a lexical catalogue read reports itself not ready, optional receipt prose is dropped), and a write must never commit on such a read.

Proof: `tests/test_write_intake.py` sends a save whose call time is spent and shows that it comes back queued and lands once, that a write the intake cannot queue and a file capture past its deadline still land, and that a second guard of a call whose first guard ran is not refused.

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
