## Context

`close-write-warning-suppression` (S2) and `add-due-state-consumers-and-carriers` (S1) are on main. Triage identity is `review_id:signal_fingerprint` in `.review-state.json` (`review_state.py`), with one composer for component fingerprints (`component_fingerprint`) that `due_state` and `apply_for_item` share. Write advisories live in their own namespace (`corpus_aware.write_advisory_ref`). The due-state emission governor (`due_state.should_emit`/`mark_emitted`) is keyed by session, audience, and vault and lives only in process memory. The bench's f23 family (`benchmarks/epistemic/fixtures/sequence2/f23-dismissal-respect.yaml`) is evaluated today only over synthetic snapshot pairs (`corpora/no_nudge.py::f23_pair`); `projectors/exomem_vault.py` declares `due_state_counters` unavailable and reads decisions from the review-state file.

Authority order for this change: current source and tests > this change's deltas > the no-nudge architecture report (Evidence page, §7/§12/§17 S6 row) > older notes. The report's field names are indicative; the deltas lock them.

## Goals / Non-Goals

Goals: a family-level "stop suggesting this" that is durable, inspectable, and auditable; the three capture primitives the paired metrics need (first-surfaced ledger, reason codes, origin tags); an emission ledger the bench can read plus batch-once emission so f23 can be run and can go green on the real runtime; a review-state store that has a stated scaling answer and a gate that proves it.

Non-goals (S7 and later): authority ceilings, prominence-derived default dispositions, the "three declines → one offer to quiet" behaviour, any automatic disposition change, the SQLite migration itself, hosted-side aggregation of metrics, any new detector.

## Decisions

### D1. Dispositions are review-state records addressed through the existing triage surface

A family disposition is recorded in the review-state store under a `dispositions` section keyed by family name, as `{disposition, reason, why, updated_at, origin}`. It is set with `triage_memory(ref="exomem://review/family/<family>", action="quiet"|"off"|"normal", why="<reason>: …")`. `normal` clears the record (mirroring `reopen`), `quiet` and `off` require a reason code. A family name is valid when it is a registered attention category (the default union plus the registered opt-in epistemic categories) or a write-advisory kind; anything else is `INVALID_REVIEW_FAMILY`.

Why the triage surface and not a new tool or parameter: the architecture forbids a new front-door tool, `ref` and `action` are free strings in the pinned schema, and S2 set the precedent of a namespaced ref (`exomem://review/advisory/…`) and a new action (`competing`) on the same surface. Why `why` still carries the human text: the disposition must be as auditable as an item decision, and a closed code alone does not say what the user actually meant.

### D2. Disposition effects are defined per surface, and sensors keep measuring

| Surface | `normal` | `quiet` | `off` |
|---|---|---|---|
| `maintain_memory(mode="audit")` findings | measured | measured | measured |
| Attention default union (`review_memory(mode="attention")`, no categories) | included | excluded | excluded |
| Explicit category review (`categories=[family]`) | included | included, annotated `disposition: quiet` | excluded unless `state="all"`, then annotated `disposition: off` |
| Due-state projection and every carrier (write, recall, bootstrap) | counted | not counted | not counted |
| Write-path advisory of that kind | emitted | not emitted | not emitted |
| Triage of an item in the family | allowed | allowed | allowed |

Exclusion is applied before fusion: the family's reasons are dropped from the report, so an item flagged only by quiet families disappears and a doubly-flagged item keeps its other reasons. That changes such an item's fused fingerprint, which is the existing "the composed signal changed" semantics and is acceptable. Per-item decisions and pair stances are untouched by a disposition; an item dismissed while its family was quiet stays dismissed when the family returns to `normal`. Dispositions are independent of prominence: changing the level never reads or writes them (S7 may later derive defaults; it reads this store).

Carrier counting treats a quiet family exactly as egress treats a withheld item — it contributes nothing, and the block's `categories` map omits it — because a count of things the user asked not to hear about is a nag by another route. Unlike egress, the disposition itself is inspectable through `review_memory(mode="dispositions")`, so the absence is explained rather than hidden.

### D3. Reason codes ride `why` as a leading token

The closed vocabulary is `intentional` (the flagged state is deliberate), `false_positive` (the detector is wrong about this page), `handled` (dealt with outside this surface), `deferred` (not worth acting on now, and not a dated snooze), `too_frequent` (the family fires more than it helps), and `unspecified`. A `why` whose first token before a colon exactly matches a code is parsed as `reason=<code>` with the remainder as free text; the store keeps `why` verbatim and records `reason` separately. No match records `unspecified` and is never an error for item decisions. `quiet`/`off` require a code other than `unspecified`.

Why not a `reason` parameter: it would be the only reason the tool input schema changes, and every existing client's free-text `why` would keep working either way. The CLI composes the prefix from `--reason`. Agents are taught the codes in bootstrap (D8).

### D4. The first-surfaced ledger records the first time a signal reached a served surface

Stored under `surfaced` keyed `review_id:fingerprint` → `{first_surfaced_at, surface}` with `surface ∈ {review, carrier, write}`. Populated when a signal is first composed into: an attention or activation report that is returned (not a withheld or disposition-excluded one), a due-state top reference that is served, or a write-path advisory that is emitted. Never populated by audit measurement, never backfilled, never written for anything egress or a disposition removed. The write is best-effort under the store lock: a read surface never fails, slows past its budget, or changes its content because the ledger could not be written; the entry is simply recorded on a later surfacing. Exposed on the wire only as `first_surfaced_at` on attention items; everything else stays in the store for metrics.

### D5. Origin is a property of who wrote the record

Every record and disposition carries `origin`: `manual` when written through the triage surface (`triage_memory`, the CLI `review` subcommands, or a governed leaf acting on an explicit decision), `automatic` when the runtime writes it itself (compaction rewrites, reconcile healing, and — in S7 — any envelope-driven disposition). Records migrated from schema v1 carry `origin: manual` because v1 could only be written by the triage surface. The manual-maintenance metric is the count of manual records whose `updated_at` falls in a window; it is computable from the store alone.

### D6. Emission is captured in the projection file, and batches emit once

`.due-state.json` gains an `emission` section `{writes, emissions, last_digest}`: `writes` increments on every projection delta (one per governed write), `emissions` when the governor marks a block emitted. The bench projector reads that section into a `due_state_counters` state item carrying `emissions` and `writes`, and the declaration flips to `available_via:due_state_file`.

A batch scope wraps every product command that can commit more than one governed write in one invocation (at minimum: `adopt_vault` apply/copy modes, `adoption_studio` apply, `maintain_memory` fix and reconcile, multi-page `compile_source`, `preserve_artifacts`, and `process_media` when it writes more than one page). Inside the scope the governor suppresses emission and the per-write projection deltas still apply; at scope exit the command's terminal carries at most one block, decided by the unchanged change-only rule. Separate tool calls are separate batches by definition: N calls that each change the counts legitimately emit N changed lines, which is the cadence the report asks for.

### D7. The store gets a schema, retention, compaction, a higher ceiling, and a gate

Schema v2: `{version: 2, records, dispositions, surfaced, stats}`. A v1 file is migrated in memory on load and written back as v2 on the next write; a v2 file is refused by older runtimes with the existing `REVIEW_STATE_INVALID`, which is the correct fail-closed behaviour for a vault-local file whose writers upgrade together. Retention: snooze records whose `until` lapsed more than 90 days ago and ledger entries older than 400 days with no standing decision are eligible for compaction. Compaction runs on write when the encoded store exceeds 1 MiB or 20,000 records, and on reconcile; it reports what it dropped in the reconcile result and never drops a standing dismissal, competing stance, or disposition. The hard read limit rises from 4 MiB to 16 MiB.

The stress gate is a test that builds a v2 store at multi-year cardinality — 50,000 decision records and 150,000 ledger entries, the shape of a decade of heavy use — and asserts that load, a decision lookup, one `apply`, and one compaction each stay within declared budgets and that the compacted file stays under the read limit. The budgets are measured on the lane and pinned with their evidence, not hand-tuned; the gate failing on a future cardinality is the declared trigger for the append-plus-compaction or SQLite migration the roadmap keeps in reserve, which this change designs for (sections are independently addressable) but does not build.

### D8. The pin moves, on purpose, through the documented rollout

The `triage_memory` and `review_memory` tool descriptions say what the surface now does (family refs, the three disposition actions, reason tokens, the dispositions view). That moves the packaged tool-surface digest. Rather than hide the actions as S2 had to, this change budgets the regeneration: `scripts/dump-tool-schemas.py`, the packaged contract, the fixture, and the ChatGPT plugin contract's pending digest with `refresh_required: true`, exactly as `docs/remote-quickstart.md` prescribes. No input parameter is added or removed.

## Risks / Trade-offs

- A `quiet` family hides true positives the user would have wanted. Mitigation: explicit category review still shows them, the dispositions view shows what is quiet and why, and the decision is reversible with `normal`.
- Ledger writes on read paths. Mitigation: best-effort, lock-scoped, failure-isolated, and measured under the retrieval latency gate; if the gate moves, the ledger write moves to the projection path only.
- Compaction drops something a metric needed. Mitigation: retention windows are long, the drop is reported, and standing decisions are never compacted.
- The pin move couples the release to a ChatGPT refresh. Mitigation: the two-phase rollout exists for exactly this; the pending digest is truthful and the release gate accepts it.

## Open Questions

None that block the slice. S7 decides prominence-derived defaults and the offer-to-quiet behaviour; the bench's production false-positive budget still waits on the calibration study.
