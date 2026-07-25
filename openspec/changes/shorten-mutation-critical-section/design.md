## Current-state call chain (verified anchors, tree = post-#320)

`remember`: `invoke_command` (`writer_lease.py:1721`) -> `LeaseManager.invoke`
(`:1249`; read bypass `:1269-1278`; warming gate `:1299-1324`) -> `idempotency.run`
with `operation_guard = mutation_guard` (`:1357-1374`; `process_media` is
already narrow via `writer_authority_guard` — the `narrow_media_commit`
precedent `:1353-1364`) -> leaf `op_remember` (`commands.py:3512`) -> `note()`
(`note.py:1305`). Inside the held boundary today:

1. `note.py:1445-1594` resolver snapshot, normalization, render, guarded reads.
2. `note.py:1488` `semantic_writes.preflight_creation` (`sw:2705`) ->
   `_evaluate_structural` (`sw:2659`, `build_corpus_context` `sc:1607`) +
   `relation_review.revalidate_prepared_creation_draft` (`rr:3174` ->
   `_attempt` `rr:2930`: corpus build AGAIN, `with_candidate` `sc:919`,
   evaluate, `_validation`).
3. `note.py:1613` `semantic_writes.commit_creation` (`sw:2770`) ->
   `commit_creation_draft` (`rr:3496`): preliminary `_attempt` + `_commit_plan`
   (`rr:3565`, 3rd validation), `ensure_manifest` (`am:552-566`, takes
   `vault_creation_lock("activation-manifest")`), `vault_creation_lock("semantic-creation")`
   (`rr:3593`) -> `_attempt` in-lock (`rr:3594`, 4th validation) ->
   `vault.batch_atomic_write` (`vault:2638`; fence `vault:2846`;
   embedding-sidecar fan-out `vault:2684-2690` — CAN LOAD THE MODEL COLD).
4. `note.py:1626-1675` advisory pass (`corpus_aware.suggest_related`, cosine
   embed — cold = minutes) after commit, still inside the boundary.

Existing-page family (`edit_memory`/`observe_memory`/`append_to_file`/
`set_frontmatter_field`/`create_file`) -> `preflight_existing` (`sw:1227`,
corpus `sw:1274`, freshness sandwich `sw:1273-1280`) + `commit_existing`
(`sw:1634`, creation lock `sw:1675`). `replace_memory` reuses
`note(_return_prepared=True)` (`note.py:236/1602`) then `commit_creation`
(`replace.py:630`).

Hoistable out of the boundary: corpus build, `with_candidate`, `evaluate`,
`_validation`, `_commit_plan` shaping, normalization, the advisory pass, and
model load. What must stay inside the boundary: `ensure_manifest`,
`vault_creation_lock` sections, `batch_atomic_write` (fence + commit + index
fan-out), the O(1) identity/artifact/destination freshness checks, and the
census recheck itself.

## Census validity token

`semantic_contract._corpus_census(root)` (`sc:1135-1210`, from
`clear-agent-facing-friction`; cached at `_CORPUS_CONTEXT_CACHE` `sc:1094`)
covers every KB/vault `.md` file plus `_access.yaml` and the two `_Schema`
registry files as `(rel_path, kind, size, mtime_ns)`, returning `None` on an
unsafe tree (symlink/reparse ancestor). Cost is one stat walk (sub-second at
2.4k pages, logged as `census_ms`). The existing delta machinery
(`_markdown_census_delta`, `sc:1286`) already bounds in-boundary
revalidation to changed pages when a full rebuild is needed.

Four gaps had to be resolved before this census could double as a reuse
token:

1. **Documented residual** — byte-identical size and identical 100ns-mtime
   in-place replacement is the same residual the corpus cache already
   accepts today (`sc:1085-1093`); accepted here too, unchanged.
2. **Identity races are structurally safe** — a new path colliding on stable
   identity flips the census (a new file entry), so the delta machinery and
   in-boundary revalidation still catch `DRAFT_ID_IN_USE` on mismatch.
3. **REAL GAP — relation-review artifacts and lifecycle sidecars.**
   `review_artifact_path` (`rr:558`), `lifecycle_decision_path` (`rr:579`),
   and `lifecycle_prepared_path` (`rr:587`) all live under
   `<KB>/_Schema/relation-reviews/`, and `_corpus_census` only walks `.md`
   files — these are JSON, so they are invisible to the plain corpus census.
   The token is therefore `(corpus_census, relation_review_census)`, where
   the second element is a scandir-census of that whole directory tree
   (flat artifact files plus the `lifecycle/<page_identity>/` sidecars),
   using the same alias-refusal safety rule as the strict corpus walk. The
   reuse path additionally keeps every artifact/lifecycle/destination
   inspection fresh at O(1) cost regardless of token match, as a second
   independent safety margin on top of the extended token. If either half
   cannot be censused cheaply, the whole token is `None`, which forces
   always-fresh in-boundary revalidation — never reuse.
4. **Sandwich capture** — the token is computed both before and after
   preflight; an unequal before/after pair collapses to `token=None`,
   mirroring the existing `freshness.triple` sandwich (`sw:1273-1280`).

## Guard narrowing (`writer_lease.py`)

Generalize the existing narrow-guard branch (`:1353-1364`, today scoped to
`process_media`) to `narrow_boundary = narrow_media_commit or
(command.name in _NARROW_BOUNDARY_COMMANDS and not
os.environ.get("EXOMEM_WIDE_MUTATION_BOUNDARY"))`; narrowed commands get
`writer_authority_guard` as their `operation_guard` instead of the full
mutation boundary. `_NARROW_BOUNDARY_COMMANDS` grows in step with the commit
sequence: `{"remember"}` first, then `+ replace_memory, edit_memory,
observe_memory`. `EXOMEM_WIDE_MUTATION_BOUNDARY` is the escape hatch back to
today's wide-boundary behavior for the whole narrowed set.

The Tier-2 file leaves (`create_file`/`append_to_file`) resolve differently
than a plain name addition: every product surface (MCP/CLI/REST) reaches
them exclusively through one consolidated command, `manage_memory_file`,
whose `operation` argument also dispatches `move`/`delete`/`recover`/`list`/
`trash-list` — routes through `commit_move`/`commit_recovery`, both
explicitly out of scope for this change and not self-guarding. Adding
`"manage_memory_file"` to `_NARROW_BOUNDARY_COMMANDS` outright would have
narrowed those too and silently dropped their only serialization. Instead, a
second predicate mirrors the existing `narrow_media_commit` precedent
exactly — `narrow_tier2_file_commit = command.name == "manage_memory_file"
and kwargs.get("operation") in {"create", "append"} and not
os.environ.get("EXOMEM_WIDE_MUTATION_BOUNDARY")` — narrowing only the two
operations that reach `commit_creation`/`commit_existing`.

## Boundary moves into the commit seam (`semantic_writes.py`)

`commit_creation` and `commit_existing` acquire
`active_manager().mutation_guard(root, request_id=active_mutation_request_id(),
operation=<command>, holder_kind="command")` around their entire bodies,
including `ensure_manifest` and the `vault_creation_lock` section. For
callers that are not on the narrowed-command list, the inner hold is
reentrant (`mutation_lock.py:207-214`) against the outer boundary they
already hold — this is what makes per-command gating safe to land as a pure
refactor before any guard actually narrows: nothing changes for a caller
still holding the wide boundary, because the inner acquire is a no-op
re-entry, not a second lock.

## In-boundary check and bounded revalidation

- **Creation.** `commit_creation_draft` splits: the preliminary `_attempt` +
  `_commit_plan` pass (`rr:3565-3589`) — pure validation, no shared state
  touched — hoists into `prepare_commit_creation_draft(...)`, which runs
  before any boundary is acquired. `_attempt` gains
  `reuse: _PrevalidatedAttempt | None`. Inside the boundary and creation
  lock, a fresh census token is computed and compared against the one
  captured when `reuse` was built; on an exact match, `_attempt` substitutes
  the pre-boundary `(before_corpus, candidate, result, validation)` in place
  of rebuilding them, but the identity/artifact/destination checks
  (`rr:2946-3025`) always execute against live filesystem state regardless
  of token match. On a mismatch (or `token=None`), `_attempt` falls straight
  through to today's full computation — unchanged behavior, just a wasted
  reuse attempt.
- **Existing pages.** On a token match, `commit_existing` commits using the
  pre-boundary preflight as today. On a mismatch, it re-runs
  `preflight_existing` warm with `expected_before_hash=preflight.before.source_hash`
  and the original `transition_token`, surfacing `STALE_SEMANTIC_WRITE` or
  `SEMANTIC_CONTRACT_BLOCKED` exactly as the existing cross-invocation replay
  path already does, then commits against the fresh result.

## Model pre-warm

Both commit paths make a best-effort `embeddings.get_model()` call before
acquiring the boundary, guarded by `EXOMEM_DISABLE_EMBEDDINGS` and
`readiness.should_defer("embeddings")`, with failures swallowed — a warm
load never blocks or fails a commit, it only removes the chance of a cold
load happening inside the boundary. `note()`'s post-commit advisory pass
(`corpus_aware.suggest_related`) exits the boundary automatically once the
guard around it is narrow, since it already runs after `commit_creation`
returns.

## Telemetry

`exomem_prevalidated_commit_total{outcome="reused"|"revalidated"|"unavailable"}`
plus a matching log event. `exomem_boundary_hold_ms` and the mutation
journal's hold fields, both added by `eliminate-mutation-busy-failure-modes`
R1, are the acceptance measurement for the narrowed hold — no new timing
mechanism is introduced here.

## Worst case and lock ordering

Worst-case in-boundary time is the token stat-walk (~50-300ms observed at
2.4k pages) plus, on a mismatch, one warm delta-reconciled revalidation
(seconds) — the hard ceiling is today's full warm validation, and that only
triggers on a concurrent config/registry change or an unsafe census, not on
the common path. Typical hold becomes census-check + creation-lock +
`batch_atomic_write` — sub-second to low seconds, down from the multi-minute
worst case observed in the 2026-07-25 incident.

Lock ordering is unchanged, now made explicit: boundary acquired before
`vault_creation_lock`, never the reverse. The `lexical-catalog-publication`
background publisher (`lexstore.py:905-927`) takes the creation lock without
the mutation boundary by design — that is unchanged. `_HELD_LOCKS` nesting
refusal (`vault.py:383-385`) already enforces no creation-lock nesting, so
narrowing the outer guard cannot introduce a new nesting hazard there.

## Untouched invariants

Single-writer semantics; fencing (`validate_active_write_fence` inside
`batch_atomic_write` under the held boundary, `vault:2846`); content-free
holder metadata; the `MUTATION_BUSY` wire shape (a busy from the inner guard
still deletes the pending idempotency row via `run`'s `leaf_returned=False`
path, identical to today); the read bypass. A pre-boundary validation
failure (`SEMANTIC_CONTRACT_BLOCKED`, `SEMANTIC_CREATION_FAILED` via
`_translate` `rr:3136-3146`, `NoteError` `note.py:1501-1502`) raises before
any boundary exists to acquire — the exact incident class from 2026-07-25
(a multi-minute hold caused by validation and a cold model load happening
*inside* the boundary) structurally cannot recur once a command is on the
narrowed list.

## Risks (ranked)

1. **Stale relation-review/lifecycle state under token reuse** — mitigated
   by the extended token plus the always-fresh artifact/lifecycle/
   destination checks on every `_attempt` call; any sidecar input that
   cannot be censused cheaply degrades the whole token to `None`, never to a
   silently-stale reuse.
2. **New `STALE_SEMANTIC_WRITE` surfacing within one invocation** for
   interleavings that previously hit `MUTATION_BUSY` under the wide
   boundary — this is semantically honest (the write really did race a
   change), not a regression, and is documented as an accepted behavior
   change in the spec delta below.
3. **Census false-match residual** — identical to today's corpus-cache
   residual; physical write safety is still enforced by `PathGuard`s and
   `create_only` at commit time regardless of what the token says.
4. **In-boundary full rebuild on a concurrent config/registry change** —
   rare, bounded at today's warm cost, and visible via the existing
   long-hold warning and `exomem_boundary_overdue_total`.
5. **Journal timing expectations** — `tests/test_mutation_journal.py` now
   measures narrower holds for narrowed commands, which is the intended
   effect; the nested-hold timing rule (`mutation_lock.py:44-53`) protects
   the outer record for any caller still holding the wide boundary.
6. **Hosted flows already holding an outer boundary see no latency gain**
   from a narrowed inner acquire (it is reentrant) — acceptable, and not a
   regression versus today.

## Out of scope

`commit_move`/`commit_recovery`, `adopt`/`adoption_run`, `audit_fix`,
`reconcile`, `knowledge_packs`, media commands (already narrow), the
`EXOMEM_MUTATION_TIMEOUT`/edge acquire budgets, the creation-lock 30s
timeout, corpus-cache internals, and any change to what the semantic
contract accepts or rejects.

## Commit sequencing

Each commit is independently gated and Windows-verifiable; see `tasks.md`
for the full checklist:

1. `docs(openspec)` — this change's artifacts.
2. `refactor` — census token, preflight plumbing, and the `_attempt`/
   `commit_creation_draft` split, with the full-leaf guard still wrapping
   everything, so behavior is bit-identical to before the refactor.
3. `feat` — narrow the guard for `remember` (`_NARROW_BOUNDARY_COMMANDS = {"remember"}`),
   wire the pre-warm and the new counter, red-first tests for the actual
   boundary-hold reduction.
4. `feat` — extend the narrowed set to `replace_memory` and the existing-page
   family, plus interleaving tests.
5. `chore` — `tasks.md` checkbox cleanup and doc touch-up; Linux CI remains
   the authoritative full-suite gate.
