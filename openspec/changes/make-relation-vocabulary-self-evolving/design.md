## Context

See [proposal.md](proposal.md) for the product motivation. The investigation traced relation authoring from Markdown and semantic-unit parsing through the registry, semantic write feedback, graph indexing, traversal and recall filters, schema inference, relation review, Studio, and the public command surfaces.

The extensible ontology substrate is real and broadly wired. Core and vault definitions share one resolver; extensions already support namespaced keys, aliases, one core parent, direction, inverse metadata, scopes, deprecation, and replacement; graph edges preserve raw, canonical, parent, status, hash, and provenance; exact-extension and parent-family filters already reach graph context and recall. Markdown can therefore author a clean alias such as `applies_to` while the stable identity remains `vault.applies_to`. A new display-label concept would duplicate the existing alias abstraction.

The missing product loop is narrower but crosses several seams:

| Lifecycle stage | Current state | Actual gap |
|---|---|---|
| Discover | Core and extension definitions are loadable; corpus inference counts vocabulary | Ordinary agents receive no intent-oriented resolver or bounded duplicate evidence |
| Author | Registered aliases work in note and semantic-unit relations | Bootstrap does not teach the registry-first, truthful-specificity loop |
| Recover from an unknown label | Full semantic write feedback identifies unregistered observations | The default compact mutation terminal removes that signal |
| Promote | Inference is proposal-first and saves are atomic/hash guarded | Unknown skeletons use bare invalid keys with null semantics; saving requires reconstructing the entire registry through a legacy infer flag |
| Activate | A rebuild correctly resolves extensions across graph surfaces | Saving the protected registry file invalidates the current graph but schedules no registry-aware graph synchronization |
| Migrate | Deprecated definitions and `replaced_by` are stored and resolved | Relation filters do not expand replacement closure |
| Review edges | Stable relation refs, fingerprints, accept, triage, and Studio already exist | Every queue read performs an activation census, a second Markdown walk, then potentially one corpus cosine pass per scanned page |

Three reproductions shape the design:

- On an anonymous roughly 3,600-page live vault, `relation-queue` with a one-page limit took about 41 seconds even though it scanned only one candidate page. The full activation census dominated.
- A synthetic 3,569-page request parsed 7,138 pages before candidate work: exactly two corpus parses per page. A 200-page no-candidate run made 200 corpus cosine calls and 400 graph snapshot attempts.
- Registering a valid alias for an already indexed unregistered synthetic edge made graph reads unavailable immediately after the save. The registry response reported only the YAML write; no derived recovery was arranged.

Historical aggregate dogfood evidence also argues against treating this only as legacy debt: generic `relates_to` use was about 58% in an earlier typed-graph audit and 53.3% in the current census. Date-bounded census support is still needed to distinguish improving cohorts without hardcoding private release dates.

## Goals / Non-Goals

**Goals:**

- Complete the existing registry loop rather than introduce a parallel ontology service.
- Give an active reasoning agent bounded deterministic evidence and keep semantic adequacy under agent authority.
- Make clean aliases the normal authoring surface while preserving namespaced canonical identity.
- Make an accepted extension become current graph semantics through recoverable derived synchronization without scanning Markdown under the registry writer boundary.
- Make relation review cost depend on the requested result budget and indexed snapshot, not on vault size times candidate generators.
- Preserve raw observations, compatibility, tenant isolation, and honest warming/truncation states.

**Non-Goals:**

- No server-side reasoning LLM, automatic semantic classification, automatic edge authoring, vocabulary quota, or automatic legacy rewrite.
- No graph database, persistent relation-vocabulary proposal queue, or second relation registry.
- No automatic inverse-edge synthesis. Inverse remains discoverable registry metadata.
- No forced promotion of every recurring unknown label. Structural metadata and legacy noise may remain unregistered or be migrated to an existing mechanism after review.
- No removal of `relates_to`, embedding proximity from explicit per-page suggestions, or the legacy full-registry save path.

## Decisions

### 1. Add one read-only resolver to the ordinary connect surface

`connect_memory(operation="resolve-relation")` is the front door because the user's intent is to connect two things, not administer a schema. It accepts:

- `query`: optional plain-language intent;
- `requested_relation`: optional clean or canonical label;
- existing `path` and `target`: optional source/target context for scoped definitions;
- `limit`: extension and observation candidate budget.

At least one of `query` or `requested_relation` is required. The same leaf is exported through MCP, REST, and CLI.

The response has four deliberately different collections:

1. `exact_matches`: canonical, normalized-label, and alias matches that must never be displaced by ranking.
2. `candidates`: bounded extensions ranked by deterministic evidence such as normalized tokens, aliases, inverse/replacement names, parent family, and description terms.
3. `core_vocabulary`: all portable core definitions. The set is small, so returning it completely lets the reasoning agent recognize non-lexical paraphrases such as “belongs to” → `part_of` without a hardcoded synonym ontology or model.
4. `unregistered_pressure`: bounded indexed raw-label counts and examples, explicitly not semantic definitions.

Each candidate reports evidence components rather than one authoritative similarity number. The response carries `extensions_total`, `extensions_returned`, `extensions_truncated`, an opaque continuation when more definitions remain, corresponding observation counts, registry versions, `relates_to` and no-edge fallbacks, and next actions. Exact canonical/alias collision checks always cover the full registry even when descriptive candidates are paginated. The continuation/inventory route makes the unexamined remainder explicit; the response never implies that a bounded page exhausted semantic alternatives. It performs no model call and no mutation.

Alternative considered: put discovery only under `schema_memory`. That preserves taxonomy but repeats the current discoverability failure in normal authoring. Alternative considered: embed every description and pick the nearest. That would make cosine look authoritative, add a dependency to a deterministic authoring preflight, and still fail on domain distinctions.

### 2. Keep aliases as the clean authoring surface

The stored extension remains namespaced. The proposal helper defaults a clean `applies_to` request to:

```yaml
vault.applies_to:
  aliases: [applies_to]
  parent: <reviewed core parent>
  description: <reviewed meaning>
  direction: <directed or symmetric>
```

The namespace can be supplied explicitly for portable packs or deliberate domain ownership. The helper requires direction even though the legacy registry format can inherit it: a directed refinement of symmetric `relates_to` is otherwise too easy to define incorrectly. Parent, description, and direction remain semantic claims supplied by the active agent; the substrate never fills them from frequency or similarity.

Alternative considered: add `display_name` or allow bare canonical extension keys. Both create two ways to represent what aliases already solve and weaken collision control.

### 3. Add proposal and delta-save operations to the existing schema command

`schema_memory(subject="relations", operation="propose-relation", proposal={...})` is read-only. Its input names the requested label, optional namespace, reviewed parent, description, direction, aliases, inverse, origins, and optional scopes. It returns:

- a complete one-extension delta;
- the current extension hash;
- registry validation findings;
- exact and near duplicate evidence from the resolver;
- observed recurrence evidence and explicit incomplete fields;
- total/returned candidate counts, truncation, and the continuation/inventory route for definitions not returned in this page.

`schema_memory(subject="relations", operation="save-relations", proposal=<delta>, expected_hash=..., why=...)` is the governed mutation. It supports upsert and deprecate/replace operations, but no hard deletion. It merges the delta with the current document and reports previous/new hashes and changed keys. Structural collisions remain hard failures; deterministic proximity remains evidence that a reviewer may knowingly override with the audit reason.

Once a canonical extension key exists, its meaning-bearing fields are immutable in place through both delta and legacy full-document saves: parent/family, description, direction, inverse, origins, source/target kinds, and scope cannot change, and aliases cannot be removed. A save may add non-colliding aliases or move an active definition to deprecated status with a valid active `replaced_by`. Once deprecated, its immediate `replaced_by` link and status are immutable. A later deprecation of that immediate target preserves the old link and extends an acyclic chain whose terminal survivor must be active; resolution reports both the immediate replacement and terminal active survivor. A semantic correction or refinement therefore creates a new canonical key and deprecates the old one. This slightly stricter rule avoids a race-prone corpus scan and, more importantly, prevents a registry edit from silently reinterpreting historical authored truth.

The legacy `operation="infer", save=true` full-document path stays compatible. Inference now emits `promotion_candidates` with valid `vault.<label>` keys and clean aliases, but null parent, description, and direction so they cannot look commit-ready. Optional `date_from` and `date_to` scope relation inference by `origin_date`: normalized frontmatter `created`, falling back to `captured`, and never to mutable `updated` or filesystem mtime. Responses report included, undated, and excluded denominators. A current graph supplies fast counts; explicit inference may retain its tolerant Markdown fallback when the graph is unavailable because it is an operator-requested census, not a hot-path action.

There is no persistent vocabulary proposal queue in this change. Unregistered observations are already durable evidence, registry saves are hash-guarded, and two concurrent proposals converge: the first changes the hash, the second must resolve again and sees the accepted definition. A new queue would add state and review semantics without solving a measured gap.

### 4. Narrow only the new relation delta-save invocation and make its graph obligation durable

The command dispatcher currently holds the broad mutation boundary for `schema_memory`. The new `save-relations` selector gets an invocation-specific narrow-boundary rule, analogous to the existing artifact and semantic-write lanes; other schema mutations keep their present behavior.

Proposal construction, duplicate evidence, and delta validation happen outside the Markdown writer boundary. The save leaf then acquires the boundary only to reload the registry, compare `expected_hash`, and give the complete registry YAML—without manually supplied epoch files—to `vault.batch_atomic_write`. The batch writer remains the sole epoch injector: `graph_sync.epoch_writes` recognizes the exact protected relation-registry target and delegates to `registry_epoch_writes`, which produces a full-scope floor/checkpoint. This preserves the existing pre-commit debt enqueue, epoch-path filtering, deferred-completion handoff, and repair checks. The registry and durable recovery demand cross the completed batch's commit point together; in-memory worker registration is post-commit scheduling, not the authority that makes recovery necessary. Incremental delta saves do not hard-delete or reinterpret existing definitions, so they need no full-corpus “observed key” scan inside the lock. The legacy full-document path adopts the same meaning-immutability and registry-epoch rules while retaining its existing deletion protection.

Derived work runs through the writer manager's existing post-guard graph synchronization phase and the same full-marker convergence dispatcher used by deferred drain/restart. A settled rebind can report `graph_sync="completed"`; otherwise the mutation returns committed registry state plus a durable `pending` result. Startup and reconcile inspect the durable epoch and full marker even if the process died before worker registration. Ordinary Markdown write latency and locking are unchanged.

Alternative considered: run graph re-resolution inside `op_schema_memory`. That would hold the only writer boundary for an O(edges) derived operation and reproduce the multi-agent contention this change is meant to remove.

### 5. Rebind registry-derived edge fields from a validated graph generation

Graph schema version increments. Each edge stores the exact resolution context already passed to the registry during indexing: project, page type, source kind, target kind, and resolver origin. It also stores the versioned, method-specific candidate-evidence inputs needed to reproduce current relation-review identities and fingerprints without reparsing Markdown. That includes raw authored body-wikilink target spelling plus an internal occurrence order/span needed to select the same first occurrence, frontmatter field and target, shared-source identity, open-question text and both unit identities, resolution target and both relation/unit identities, and semantic-unit lift family and bounded authored-unit evidence. Internal location/order fields do not expand the public evidence object: for example, body-wikilink evidence remains exactly `{"source_path", "target"}`. File nodes project queue/census fields into indexed columns: page type, lifecycle status, tags, project, immutable-best-effort `origin_date` (`created` then `captured`), mutable `updated`, access/eligibility classification, source hash, and activation signal version. Existing JSON metadata remains for compatibility and explain output, but parity tests compare the exact public evidence object, stable ref, signal version, fingerprint, and dismissal behavior before and after the schema switch.

Registry rebind follows this protocol:

1. The canonical batch commits the registry YAML together with the next full-scope graph epoch floor/checkpoint. The old registry hash is obtained from the prior graph snapshot and the new hash from the committed YAML; the checkpoint generation binds the recovery demand even if no worker is ever registered in memory.
2. One full-marker convergence dispatcher serves both live post-guard work and `index_sync` deferred drain. Before choosing work, it obtains the canonical mutation boundary long enough to sample a settled epoch, current registry hash, graph generation/hash, and the observed full-marker generation. If the boundary is busy or the epoch is mid-publication, it leaves the marker untouched rather than starting from the pre-commit enqueue.
3. Outside the mutation boundary, the dispatcher chooses registry rebind only when the graph registry hash differs from the current registry and the old graph has compatible schema, current source manifest, and stable instance/generation. Otherwise it runs the existing full rebuild.
4. For rebind, it copies that snapshot to a private candidate, resolves every stored raw relation with the new registry and stored context, and updates only canonical relation, parent, status, replacement/findings metadata, registry version/hash, and relevant graph meta.
5. It atomically publishes only if the source generation and checkpoint are still authoritative. A concurrent newer graph job supersedes or coalesces it through the existing graph-sync state machine.
6. After either successful rebind or full rebuild publication, it acknowledges the checkpoint and compare-and-swap clears only the observed full-marker generation. A newer marker survives. Live success and restart drain therefore cannot leave a redundant rebuild behind or erase later debt.

Edge keys, endpoints, raw labels, exact candidate evidence, source anchors, source hashes, and authored metadata do not change. Rebinding therefore activates historical unknown aliases and deprecations without parsing or rewriting Markdown. Readers already reject an extension-hash mismatch; they continue to return a typed warming/pending state until publication.

Crash-cut tests cover the actual ordered-batch authority boundaries. A caught failure before the commit point restores registry and epoch, though pre-commit debt may remain as harmless rebuild work. An abrupt stop during floor/registry/checkpoint publication may expose either the old state or a newer floor with an older checkpoint; that state is intentionally classified as recoverable and must never look current. A stop after the completed batch but before worker registration is recovered solely from the durable epoch/full marker on restart. An interruption while a private graph candidate is publishing never acknowledges the epoch unless one coherent new snapshot became current. A drain awakened by the pre-commit marker cannot pass the canonical-boundary/settled-epoch admission before the registry batch finishes. Every durable post-registry cut must converge through rebind or full rebuild without a second registry write.

Alternative considered: always schedule a full graph rebuild. It is correct and remains the fallback, but it reparses the full vault for a registry-only change even though every raw observation and resolution input can be preserved in the derived sidecar.

### 6. Build survivor-directed replacement closure once in the registry

Registry loading validates immutable immediate `replaced_by` links, acyclic chains, and one active terminal survivor, then materializes predecessor sets for each terminal survivor. Relation filter planning first resolves a requested alias to its canonical key, then expands:

- the requested canonical key;
- extension descendants when the requested key is a core family;
- deprecated predecessors only when the requested key is their active replacement/survivor.

A query for a deprecated key remains historical: it returns observations stored under that key, reports its active replacement and deprecation warning, but does not pull in successor observations. This matters when a narrow extension was deprecated to a broad core relation such as `relates_to`; symmetric “equivalence” would otherwise make the historical narrow query return every broad generic edge. True bidirectional equivalence would require a separate explicitly reviewed alias/merge contract and is out of scope.

Graph context, traversal, recall fusion, and schema/debug helpers use the same expansion. Returned edges keep `raw_relation` and stored `relation_type`. Existing `matched_via="relation_type"` and `matched_via="parent_relation"` remain stable; the additive value `matched_via="replacement"` applies only when an active survivor query reaches a deprecated predecessor. Alias normalization is reported separately as `requested_relation` and `resolved_relation`. Match precedence is `relation_type`, then `replacement`, then `parent_relation`, so one edge receives one deterministic explanation.

### 7. Project the existing unregistered feedback through the compact terminal

Semantic validation already resolves every authored relation and produces unregistered facts. The implementation adds one central projection in `mutation_terminal.py`, not per writer. A successful committed terminal receives at most one bounded `relation_advisory` containing unique raw labels, the registry hash, locally available counts, truncation, and a `resolve-relation` next action.

The hot path uses only the just-parsed write facts and an indexed point/count lookup when the graph is current. It never scans the corpus or embeds descriptions. Failure to obtain recurrence evidence leaves the advisory useful with `recurrence_available=false`; it never delays or fails the write. Registered labels, `relates_to`, and no-edge writes receive no specificity nag.

### 8. Make the relation queue a bounded query over one graph snapshot

The graph already materializes all deterministic inputs used by the queue. The schema bump adds indexes for review eligibility, source-path/origin/relation joins, and unregistered raw-label recurrence. Queue assembly opens one current snapshot and executes a fixed query plan that unions these existing methods in their current precedence:

1. semantic-unit relation lift;
2. shared open question;
3. shared resolution target;
4. body wikilink;
5. frontmatter source;
6. shared source.

Embedding proximity is intentionally removed only from the batch queue. It remains available in `connect_memory(operation="suggest-relations", path=...)`, where one page was explicitly requested and model availability is already surfaced.

The SQL plan computes activation-equivalent eligibility and coverage from indexed page/edge/unit fields, suppresses already authored edges and missing targets, and applies per-method and per-source caps before materialization. Review state is loaded once, canonical memory refs are resolved in one batch, and a bounded over-fetch is filtered in memory. The batch reconstructs the exact versioned public candidate evidence object from the indexed method-specific inputs described above; it does not substitute a merely similar graph fact. Stored activation signal versions plus those exact evidence bytes preserve stable refs, fingerprints, and prior dismissals for unchanged candidates. The response keeps current group/item shapes, ordering, stable refs, fingerprints, coverage, and explicit truncation; it adds availability and mandatory source hints for new decisions. No queue request reconstructs the graph or falls back to a corpus walk. An unavailable graph returns a typed empty warming/pending response promptly.

Alternative considered: cache or persist the relation queue separately. That introduces another projection to invalidate on every page and review-state change. A bounded indexed query over the existing materialized graph is simpler and keeps one source of derived truth.

### 9. Revalidate new decisions from a mandatory source hint, with bounded legacy compatibility

Queue items carry `source_path` as well as the existing group path and source hash. `accept-relation` reuses its existing `path` argument as the required source hint for newly returned items; `triage_memory` gains a required `source_path` for those items. Candidate regeneration queries one source's graph neighborhood and rereads at most that page for live eligibility/hash validation. Ref, fingerprint, hash, and audit-reason guards remain unchanged. MCP, REST, CLI, and Studio all echo the hint supplied by the queue rather than asking a user to discover it.

The old ref hashes only `(from, to, relation_type, method)` and are not invertible, so ref-only compatibility cannot promise arbitrary lookup without a corpus-sized candidate generation. A hintless legacy request searches only the same bounded current queue prefix that a normal queue read would return. If the ref is absent—including a deterministic candidate outside that prefix or a retired embedding-only candidate—the server returns a stable refresh-required result while preserving any review-state record. It never widens the prefix, walks Markdown, or recomputes embeddings. This is an explicit bounded compatibility limit, not a claim that every legacy ref remains actionable.

### 10. Teach the loop without loading a private ontology into bootstrap

Compact bootstrap adds the full portable core keys, relation contract version, extension hash/count, and the resolve/propose/save workflow. It does not inline an unbounded vault registry. Authenticated resolution and schema inventory return extension definitions for the addressed vault. The generic scaffold and workflow skills use only synthetic examples and explicitly teach:

- specific and truthful beats generic;
- generic and no edge beat invented semantics;
- resolve before propose;
- propose only for a durable or recurring distinction;
- clean aliases are authoring labels, canonical names remain namespaced;
- registry changes are proposal-first and hash guarded.

### 11. Measure structural cost separately from calibrated latency

Unit and integration regressions assert stable work bounds with counters: one graph snapshot, fixed query count, zero corpus Markdown parses, zero embedding calls, no writer-boundary acquisition, and O(1) source-page reads for hinted decisions. Concurrency tests coordinate a held writer and graph publication with events/barriers, not fragile short timeouts.

A dedicated Linux benchmark creates at least 3,600 synthetic eligible pages, prebuilds the graph, and runs twenty mixed request streams. Mutation actors change canonical Markdown on eligible graph pages—no validation-only, no-op, or graph-excluded substitute—and every successful mutation must advance a real graph checkpoint. An explicit barrier overlaps queue reads with successful graph-relevant commits during the mixed phase. The harness reports queue p50/p95/max separately for `available` responses and typed unavailable responses, mutation outcomes, graph availability ratio, post-mutation recovery latency, query/snapshot/parse/embed counts, checkpoint generations, and candidate denominators. A passing run has at least two committed graph-relevant mutations, at least 90% available queue completions, and at least one current available queue completion within 5,000 ms after the final graph-relevant commit; an all-busy writer run, all-warming reader run, or phase with non-overlapping reads/writes fails. For twenty-group requests, available responses have p95 below 1,000 ms and maximum below 2,000 ms, while typed unavailable responses have p95 below 250 ms. Runs happen on a quiesced benchmark machine except for the workload's own controlled concurrency.

The graph-value corpus adds seven semantic cases: policy applicability extension, core `part_of` reuse, extension alias reuse, honest generic connection, false-specificity abstention, parent roll-up, and deprecation migration. Because the resolver is deliberately non-authoritative, the fixture separates evidence from a tiny scripted caller decision: it asserts that `part_of` is visible and no mutation happens during resolution, then explicitly authors `part_of`; for topical proximity it asserts that resolution selects and writes nothing unless the scripted caller deliberately chooses an honest generic edge. The benchmark never treats a candidate's mere presence or rank as an automatic semantic decision. A two-vault case checks hosted isolation. All fixtures are synthetic.

### 12. Treat historical relation debt as cohort evidence, not a write target

The explicit relation census can group authored core, extension, generic, deprecated, unregistered, and disconnected-page counts by caller-supplied `origin_date` range and page type. `origin_date` remains `created`, then `captured`, with undated pages reported separately; product release cutoffs are caller data, not hardcoded ontology. The report distinguishes zero authored relation rows from zero body connections and reports denominators, because a standalone note is not automatically defective and `relation-debt` is not a graph-quality score.

Legacy edge adoption uses the existing proposal-first relation queue once it is graph-native and responsive. It may surface deterministic candidates for old pages, but it never auto-connects them or promotes their raw labels. Structural legacy tokens such as `parent` and `sibling` remain unregistered pressure until an agent determines whether they map to existing composition, another metadata mechanism, or a genuinely durable semantic extension. This avoids a second backfill queue and keeps vocabulary governance separate from edge acceptance.

## Compatibility and Surface Map

| Surface | Compatibility action |
|---|---|
| Markdown / semantic units | No grammar change; clean aliases and namespaced keys continue to parse |
| Registry YAML | Schema version 1 remains readable; delta save emits the same complete document shape; existing canonical meanings are immutable in place |
| Graph sidecar | Rebuildable schema bump; new resolution/queue columns and indexes are derived only |
| Graph/traversal/recall filters | Same inputs; active terminal-survivor queries add deprecated predecessors with `matched_via="replacement"`; deprecated-key queries remain exact and report immediate/terminal replacement |
| `schema_memory` | Existing infer/validate/diff and infer-save remain; new proposal/delta-save selectors are additive |
| `connect_memory` | New read-only selector and additive `requested_relation` argument |
| Relation queue | Groups, methods, refs, fingerprints, and review state remain when exact indexed evidence is unchanged; new decisions require source hints; hintless legacy refs are bounded-prefix lookup or refresh-required |
| MCP / REST / CLI | Generated from the same command signatures and leaves; fixtures regenerate together |
| Studio | Existing worklist gains explicit warming/truncation and sends source hints |
| Hosted | Registry, resolver caches, graph snapshots, and review state remain keyed by tenant vault/cell |

## Implementation Shape

The implementation is split into dependency-aware lanes for `$lane-delegate`; only ready, non-overlapping lanes run concurrently:

```text
A vocabulary
    ↓
B graph + durable registry epoch
    ↓
┌───────────────┐
│               │
C queue         D public/mutation/agent contract
│               │
└───────┬───────┘
        ↓
E Studio
        ↓
F lifecycle/scale/census/docs
        ↓
final integration verification
```

Every implementation lane starts with a failing focused test or reproduction, owns disjoint production/test files where possible, changes only its allowlisted scope, and receives a fresh author-independent review before orchestrator acceptance. Lane implementers do not commit, merge, push, or edit OpenSpec; the orchestrator integrates accepted diffs and may make local dependency-base commits so later worktrees contain reviewed bytes. Nothing is pushed until the integrated delivery gates pass. Shared signature/generated-schema integration is serialized in one lane to avoid fixture conflicts.

| Lane | Primary production ownership | Primary test/artifact ownership |
|---|---|---|
| A — vocabulary | new `relation_vocabulary.py`; pure resolution, merge, validation, and continuity helpers in `relation_registry.py`; proposal/inference helpers in `memory_schema.py` | new resolver tests plus registry/schema tests |
| B — graph | `epistemic_graph.py`, `graph_sync.py`, `index_sync.py`, `deferred_index.py`, `find.py`, `traversal_profiles.py`, and optional new registry-rebind helper; owns automatic registry-epoch injection, full-marker convergence/CAS cleanup, rebind, and survivor-filter contracts | graph schema/rebind/filter/review-batch/deferred-drain/crash-cut tests |
| C — queue | `relation_queue.py` | relation queue and relation queue command tests |
| D — integration | `commands.py`, the `relation_registry.save_registry` persistence seam, `writer_lease.py`, `mutation_terminal.py`, egress/command schemas, bootstrap/scaffold, generated public fixtures; consumes Lane A validation and Lane B's automatic epoch/convergence contracts without changing the batch writer | command-surface, writer, terminal, bootstrap, scaffold, and schema-parity tests |
| E — Studio | versioned files under `src/exomem/studio/` | Studio model, governed-flow, route, and browser tests |
| F — evidence | graph-value and new relation-review/census scripts, synthetic manifests, public docs | benchmark/census tests and aggregate reports |

The delegation packet is an orchestrator-owned scratch artifact carrying the absolute approved-plan path and SHA-256, base revision, exact allowlist, isolated state/temp roots, red tests, gates, forbidden actions, and stop conditions. It narrows these rows after the red tests identify the required seams. Lane A is integrated before B; reviewed B is then the shared dependency base for concurrently ready C and D; both accepted diffs are integrated before E starts; F starts only from accepted E. C owns the source-hinted queue leaf while D owns public argument dispatch against the pinned interface; each can unit-test its side with real-shaped fixtures, and the combined C+D integration test is mandatory before E. A file needed by two rows is reassigned to the earlier dependency or changed later by the serialized orchestrator integration; workers do not widen their own scope.

## Risks / Trade-offs

- **[Graph rebind lacks exact scope context]** → Persist every resolver input on edges during the schema bump and parity-test rebind against a clean full rebuild across scoped, aliased, deprecated, semantic-unit, and unresolved-target cases.
- **[Registry save bypasses batch epoch accounting]** → Let `graph_sync.epoch_writes` detect the exact registry target; callers supply only YAML, while `vault.batch_atomic_write` retains epoch injection, pre-commit debt, fanout filtering, and handoff ownership.
- **[Pre-commit full debt races the registry batch or survives a fast rebind]** → Admit full-marker work only after sampling a settled epoch under the canonical mutation boundary; route live and deferred work through one rebind-or-rebuild dispatcher; CAS-clear the observed marker after either successful publication.
- **[Process dies after registry replacement but before graph work is registered]** → Treat the durable epoch/full marker, not in-memory registration, as authority; recover through the shared rebind-or-full-rebuild dispatcher on startup.
- **[Graph-native review evidence is similar but not fingerprint-identical]** → Persist every method-specific evidence input, including raw body-wikilink target/anchor, and parity-test public evidence bytes, refs, signal versions, fingerprints, and dismissal behavior against the pre-change generator.
- **[A deprecated narrow key points at a broad survivor]** → Expand replacements only from active survivor to deprecated predecessors; keep deprecated-key queries historical and exact.
- **[A replacement target is deprecated later]** → Freeze each immediate replacement link, permit only acyclic chains with one active terminal survivor, and expose both immediate and terminal replacement diagnostics.
- **[An in-place registry edit changes what old edges mean]** → Freeze meaning-bearing fields and alias removal for existing canonical keys; require a new key plus deprecation/replacement for semantic evolution.
- **[A fixed SQL plan still scans large indexes]** → Add covering indexes, cap every union branch before Python materialization, measure query plans/counters at 3,600+ pages, and retain explicit truncation rather than chasing complete totals synchronously.
- **[Removing embedding proximity from the queue reduces discovery breadth]** → Keep it on explicit per-page suggestions and document the queue as high-evidence deterministic review rather than every possible neighbour.
- **[Complete core vocabulary adds response bytes]** → The portable set is currently 28 entries and bounded by release; extensions remain capped and separately inventoried.
- **[Near-duplicate semantic definitions still require judgment]** → Surface multiple independent signals and require audited proposal/save. Do not let a scalar score approve or reject ontology meaning.
- **[Date fields are absent or unreliable on legacy pages]** → Report included, undated, and excluded denominators; never silently assign an era from filesystem mtime.
- **[Studio changes regress accessibility or responsive behavior]** → Preserve server semantics in model tests, then exercise keyboard, narrow, and desktop states in Chrome DevTools before delivery.

## Migration Plan

1. Ship additive resolver/proposal/delta-save contracts and regenerated MCP/REST/CLI schemas while retaining legacy calls.
2. Bump and rebuild the derived graph schema on first use; old sidecars remain disposable and Markdown remains canonical.
3. Enable durable full-scope registry epochs and registry rebind scheduling for every accepted registry save. A vault with no current graph or a restart before worker registration receives the normal rebuild-pending recovery state.
4. Switch the relation queue to graph-native assembly. During graph warming it returns typed unavailable state rather than the legacy scan.
5. Enable compact relation advisories and bootstrap/scaffold guidance after the resolver route exists.
6. Ship Studio handling after the server payload is stable.

Rollback removes the new selectors/advisory/Studio behavior and rebuilds the derived graph with the prior schema. Registry YAML and Markdown need no rollback or rewrite; accepted extensions remain valid under the pre-change registry loader. Survivor-directed replacement expansion is additive and can be disabled without changing stored observations.
