## Context

The local server binds transport before background activation. Since #795, activation starts retrieval warm-up, waits for retrieval-catalog and semantic-corpus admission, and only then starts the file watcher. The watcher is the owner of the event-maintained recall projection, so this ordering guarantees that startup catalogue verification observes a non-live projection and takes the full policy-projected vault-walk fallback. On Windows that fallback repeats no-follow validation and long-path canonicalisation for every candidate; the production profile spent about 49 seconds and 38.8 million calls there.

The failure is persistent when the one-shot catalogue warm fails. `finish_warm()` changes retrieval admission to `unavailable`; the watcher may subsequently seed and catalogue repair may converge, but no transition promotes retrieval back to ready. Server `find` then returns `RETRIEVAL_INDEX_WARMING` indefinitely. Vector-only reads can also bypass the admission guard and reach `FreshnessSnapshot.recall_paths()`, where a non-live projection can still run the full walk on the request thread.

Earlier changes already provide the right building blocks: event-maintained freshness maps, checkpoint-bound catalogue rows, bounded delta repair, lexical-sidecar resolver entries, and retryable readiness outcomes. This change restores their intended ownership rather than adding another cache.

## Goals / Non-Goals

**Goals:**

- Make the watcher-owned recall projection authoritative before startup catalogue verification uses it.
- Make every server retrieval mode fail fast or serve a declared degraded result; no server request may perform the full policy-projected vault walk.
- Let a later successful seed/catalogue repair promote a failed startup admission to ready without restarting the process.
- Preserve exact Records/Planning suppression, access-policy fingerprints, Windows 8.3 alias protection, no-follow validation, and checkpoint-bound publication.
- Attribute projection/admission work in retrieval timings and pin the request-path complexity with deterministic tests.
- Preserve explicit offline/CLI correctness when no long-lived runtime owns an event projection.

**Non-Goals:**

- Replacing BM25, vector, graph, reranker, or CLIP retrieval architecture.
- Weakening access or path validation to gain speed.
- Making graph, referent, or model-backed enrichment a readiness requirement for ordinary lexical recall.
- Moving production-sized latency suites into ordinary pull-request CI; that is a separate CI-policy change.

## Decisions

### 1. Start observation and projection seeding before retrieval warm-up

`LocalRuntimeActivation` will start the file watcher first. `FileWatcher` will expose a process-local seed-completion signal reporting whether both recall scopes became live and every event observed during the seed was replayed. Activation will wait on that condition before starting catalogue warm-up when event indexes are enabled. Graph drain and media work remain behind actual retrieval admission, including after a terminal first catalogue-warm failure, so they cannot contend with recovery of the critical seed/catalogue path. They also serialize behind semantic-corpus work while the initial warm remains active, but a terminal semantic soft-failure cannot strand them because that component has no independent repair signal. A successful later catalogue repair releases them without requiring a restart.

Observation is armed before the seed begins. Events seen while the seed is walking are retained in the dispatch buffer and replayed only after the authoritative replacement maps are published; the seed-completion barrier is released only after that catch-up flush finishes, so a stale seed snapshot cannot overwrite an edit or admit its catalogue during startup. If watchdog is missing or cannot start while event indexes remain enabled, the runtime stays projection-first through reconcile-only polling rather than silently losing event-index maintenance. If event indexes are explicitly disabled, startup continues through the existing background walk fallback. The transport stays live throughout; the fallback remains background work and never migrates to a request thread.

Alternative rejected: start warm-up and watcher concurrently. That retains a race in which both can perform the same full walk and makes the result machine-speed dependent.

### 2. Bind server reads to live projection admission

All `find` modes, including vector-only mode, will consult retrieval admission. A runtime in `warming` or `unavailable` state cannot construct a request snapshot that is allowed to walk. `FreshnessSnapshot` will carry an explicit projection policy: server requests require a live projection; offline/CLI callers may retain the current bounded correctness fallback. Admission produces an exact pair of catalogue-matching projection checkpoints and pins that proof into the request snapshot. If either projection advances before its paths are copied, the request declines and retries after repair rather than mixing a catalogue from one generation with an allowlist from another.

If readiness says ready but the required projection is not live or its catalogue checkpoint no longer matches, the request will downgrade admission, schedule repair, and return the existing retryable warming outcome rather than walking. Resolver acquisition uses only the already-live checkpoint and never re-enters the reprojecting checkpoint seam. Vector/hybrid expansion inherits the same strict snapshot policy. Optional referent enrichment receives the primary find's exact checkpoint proof; it omits itself if that generation advances instead of acquiring a second proof and mixing generations. A cold referent registry is omitted and single-flighted onto a background thread rather than enumerated by the request. This makes the invariant local and testable instead of trusting startup order alone.

Alternative rejected: delete the fallback globally. CLI, maintenance, tests, and deliberately watcher-free deployments still need a correct source-of-truth path.

### 3. Make readiness recoverable from converged derived state

A read-only promotion helper will verify both KB and vault catalogues against their live recall checkpoints without walking or rebuilding. Startup warm, watcher seed completion, background catalogue repair completion, and an unavailable request's cheap preflight may call it. Only complete checkpoint matches mark `retrieval_catalog` ready.

Promotion is monotonic for a proven checkpoint. A later projection loss or mismatch demotes retrieval before serving; repair must prove the new state before promotion. The health endpoint therefore reports transport liveness separately from partial/full retrieval admission.

Alternative rejected: set readiness directly when the watcher seed finishes. A live projection alone does not prove that the maintained lexical catalogue represents it.

### 4. Keep heavy and model-backed lanes optional

Lexical catalogue/projection admission is the minimum useful read surface. Embeddings, reranker, CLIP, graph, and referent enrichment remain optional and soft-failing, and their warming cannot revoke lexical recall once admitted. Embedding and reranking models remain deterministic measurement components under the pure-substrate boundary; no reasoning model is introduced.

The existing rollback switches keep their declared behavior. `EXOMEM_DISABLE_WARMUP=1` leaves the process in the unverified lazy mode instead of manufacturing a permanently warming managed runtime. `EXOMEM_DISABLE_EVENT_INDEXES=1` retains the legacy walk-backed lazy request path while any catalogue verification remains background work. These are explicit operator choices, not accidental fallbacks from the normal managed-server path.

### 5. Measure the invariant directly

Focused tests will reproduce the exact ordering and stranded-readiness failure, prove vector mode cannot bypass admission, and make the full-walk seam raise if a server request reaches it. Timing output will claim projection acquisition explicitly rather than leaving it in `unattributed_ms`.

Production acceptance will use distinct uncached queries on the real Windows vault after a restart. Repeated identical cache hits are not acceptance evidence.

## Risks / Trade-offs

- **Watcher seed failure could delay catalogue warm-up** → Wait on a condition with a bounded terminal result; on failure, continue with the existing background fallback and explicit unavailable readiness.
- **Starting the watcher earlier could let downstream indexing contend with startup** → The observer may record events, but graph/media/deferred drains remain gated until required admission; seed itself publishes only the projection baseline.
- **Readiness and projection state could diverge after startup** → Enforce the live-projection check at the request boundary and demote before serving.
- **A repair callback could create import cycles or repair storms** → Keep promotion read-only, idempotent, and single-flight behind existing catalogue repair scheduling; readiness owns state transitions.
- **Watcher-free CLI behavior could regress** → Preserve the walk-enabled default outside activated server requests and add explicit offline coverage.

## Migration Plan

1. Ship the code with no sidecar schema change; existing recall checkpoints and catalogue rows remain valid.
2. On restart, bind transport, start watcher observation/seed, verify catalogue state, then admit retrieval and start graph/media work.
3. Verify `/health/ready` transitions from warming to admitted and stays admitted after catalogue repair.
4. Run distinct keyword and hybrid live-vault queries and confirm no request-path full walk, no warming refusal after convergence, and subsecond steady-state latency.
5. Roll back by reinstalling the prior wheel; all changed state is rebuildable process-local readiness/projection state, so no canonical-vault migration is required.

## Open Questions

None blocking implementation. Persisted degraded catalogue serving may be considered separately if startup partial results are still needed after the critical path is made bounded; this change does not silently serve a stale catalogue.
