## 1. C1 — docs(openspec): change artifacts

- [x] 1.1 `proposal.md`, `design.md`, `.openspec.yaml`.
- [x] 1.2 `specs/hosted-mutation-safety/spec.md` (MODIFIED) and
      `specs/write-latency/spec.md` (ADDED).
- [x] 1.3 `tasks.md` (this file).

## 2. C2 — refactor: census token + preflight plumbing (no boundary movement)

- [x] 2.1 `semantic_contract.corpus_validity_token(root)`: extends
      `_corpus_census` with a scandir-census of the relation-review artifact
      and lifecycle sidecar directories (`_Schema/relation-reviews/`);
      returns `None` on an unsafe tree or when either half cannot be
      censused cheaply. Red-first unit tests: unsafe tree -> `None`;
      review-artifact change flips the token; config change flips the token.
- [x] 2.2 `CreationPreflight.census_token` / `ExistingPreflight.census_token`
      (default `None`), captured as a before/after sandwich around
      `preflight_creation` (`sw:2705`) and `preflight_existing` (`sw:1227`);
      an unequal sandwich collapses to `None`, mirroring the
      `freshness.triple` sandwich (`sw:1273-1280`).
- [x] 2.3 `relation_review.py`: split `commit_creation_draft` (`rr:3496`) —
      hoist the preliminary `_attempt` + `_commit_plan` pass (`rr:3565-3589`)
      into `prepare_commit_creation_draft(...)`. `_attempt` (`rr:2930`)
      gains `reuse: _PrevalidatedAttempt | None = None`; on a matching fresh
      token it reuses `(before_corpus, candidate, result, validation)` and
      skips only `build_corpus_context`/`build_page_state`/`evaluate`/
      `_validation` — identity/artifact/destination checks
      (`rr:2946-3025`) always run fresh; on a mismatch it falls through to
      today's full path, unit-tested directly.
- [x] 2.4 `semantic_writes.py`: extract the revalidate-on-mismatch helper
      for `commit_existing` — re-runs `preflight_existing` warm with
      `expected_before_hash=preflight.before.source_hash` and the original
      `transition_token`. Present and unit-tested this commit; not yet
      reachable from a production call path (wired in C4).
- [x] 2.5 Gate: `tests/test_semantic_creation_writers.py`, `tests/test_note.py`,
      `tests/test_corpus_context_cache.py`, `tests/test_replace.py`, plus the
      new token/reuse/revalidation unit tests, all green; the full-leaf
      mutation guard still wraps every path this commit touches, so verdicts
      and behavior are bit-identical to before the refactor.

## 3. C3 — feat: narrow the boundary for `remember`

- [x] 3.1 `writer_lease.py`: generalize the `narrow_media_commit` branch into
      `_NARROW_BOUNDARY_COMMANDS = {"remember"}` plus the
      `EXOMEM_WIDE_MUTATION_BOUNDARY` kill switch.
- [x] 3.2 `relation_review.py`: split `commit_creation_draft` further —
      `commit_prepared_creation_draft(prepared, ...)` now takes an
      already-`prepare_commit_creation_draft`-validated draft and does only
      `ensure_manifest` + the creation lock + the in-lock `_attempt`/
      `_commit_plan` + the atomic write; `commit_creation_draft` becomes a
      thin one-call wrapper composing both phases for callers that don't
      narrow their boundary. `_commit_plan` gains a `reuse_result` hint so
      its own reviewed-none re-evaluation is also skipped on a proven
      census-token match (the redundant work `_attempt`'s own `reuse`
      parameter did not cover). `semantic_writes.commit_creation` calls
      `prepare_commit_creation_draft` before acquiring
      `active_manager().mutation_guard(...)`, then
      `commit_prepared_creation_draft` inside it — so the boundary covers
      only `ensure_manifest` + the creation lock + the commit, not the
      pre-commit corpus/relation-review validation.
- [x] 3.3 Embeddings pre-warm (`semantic_writes._prewarm_embeddings`) before
      boundary acquisition in `commit_creation`, guarded by
      `EXOMEM_DISABLE_EMBEDDINGS` + `readiness.should_defer("embeddings")`,
      failures swallowed.
- [x] 3.4 `exomem_prevalidated_commit_total{outcome=...}` counter + the
      `prevalidated_commit` cataloged log event, emitted from
      `relation_review.commit_prepared_creation_draft`'s in-lock `_attempt`
      call site.
- [x] 3.5 Red-first tests in `tests/test_shorten_critical_section.py`:
      bounded hold under a slow validator (bug-injection-verified: the
      hold-covers-validation bug this change fixes reproduces as red),
      pre-boundary failure never acquires (bug-injection-verified) with a
      baseline-identical error, embeddings pre-warm observes a free
      boundary, `MUTATION_BUSY` shape unchanged under a narrow hold,
      census-mismatch fallback reachable and exercised end-to-end for
      `remember`. Concurrent-writers coverage deferred — the single-mismatch
      and single-busy-contender tests already exercise the relevant
      interleavings; a dedicated multi-thread `remember` stress test can
      follow in C4 alongside the existing-page family's own concurrency
      tests.
- [x] 3.6 Gate: `tests/test_writer_lease.py`, `tests/test_mutation_lock.py`,
      `tests/test_command_surface_retry.py`, `tests/test_note.py`,
      `tests/test_note_suggestions_knob.py`, `tests/test_mutation_journal.py`,
      `tests/test_shorten_critical_section.py`; plus
      `tests/test_semantic_creation_writers.py`,
      `tests/test_corpus_context_cache.py`, `tests/test_replace.py`.

## 4. C4 — feat: extend to `replace_memory` and the existing-page family

- [x] 4.1 `_NARROW_BOUNDARY_COMMANDS` grows to
      `{"remember", "replace_memory", "edit_memory", "observe_memory"}`
      (resolved from `_PRODUCT_SPEC` in `commands.py`: every one of
      `edit_memory`'s four edit/multi_edit/set_take/set_frontmatter_field
      sub-modes and `observe_memory` route to `commit_existing`;
      `replace_memory` routes to `commit_creation`, already self-guarding
      since C3). The Tier-2 file leaves (`create_file`/`append_to_file`) are
      exposed only through the single consolidated `manage_memory_file`
      product command (resolved from `_SPEC` vs. `_PRODUCT_SPEC`: `_SPEC`'s
      raw per-leaf names are not reachable through any live invocation path
      today — `_PRODUCT_SPEC`/`PRODUCT_COMMANDS` is what every surface
      dispatches through). Since `manage_memory_file` also carries
      `move`/`delete`/`recover`/`list`/`trash-list` (routed through
      `commit_move`/`commit_recovery`, explicitly out of scope and not
      self-guarding), a blanket add would have narrowed those too and lost
      their only serialization. Added a `narrow_tier2_file_commit` predicate
      instead, mirroring the existing `narrow_media_commit` precedent
      exactly: `command.name == "manage_memory_file" and
      kwargs.get("operation") in {"create", "append"}` (plus the kill
      switch) — narrows only the two operations that reach `commit_creation`/
      `commit_existing`, leaving move/delete/recover/list on the wide
      boundary unchanged.
- [x] 4.2 `commit_existing` acquires the mutation boundary around its full
      body (fresh census-token check, `_revalidate_existing_preflight` on a
      mismatch or `census_token=None`, `manifest_install_required`,
      resolver-freshness priming, the creation lock, and the atomic write —
      all inside); embeddings pre-warm before acquisition, mirroring
      `commit_creation`.
- [x] 4.3 Interleaving tests for the existing-page family in
      `tests/test_shorten_critical_section.py`: two concurrent `edit_memory`
      writers on different pages both succeed under the narrow boundary (one
      benign `PATH_GUARD_CHANGED` retry on a shared log/index auxiliary is
      accepted and documented as a new, honest interleaving this change can
      surface — never silent corruption); a sibling write racing between
      `preflight_existing` and `commit_existing` is caught by the census
      mismatch and revalidated, surfacing `STALE_SEMANTIC_WRITE` exactly as
      the existing cross-invocation path already did (bug-injection-verified
      red: without the wiring this same race instead falls through to the
      less specific PathGuard-level `PATH_GUARD_CONTENT`). Census-mismatch
      fallback for creation was already covered in C3
      (`test_remember_commit_revalidates_on_a_census_mismatch_between_prepare_and_commit`)
      — not duplicated.
- [x] 4.4 Gate: the C3 suite set plus `tests/test_semantic_creation_writers.py`,
      `tests/test_mutation_concurrency.py`, `tests/test_corpus_context_cache.py`,
      `tests/test_replace.py`, and the edit/observe suites
      (`test_edit_heading.py`, `test_edit_operations.py`,
      `test_edit_surgical_replace.py`, `test_edit_validate_only.py`,
      `test_get_edit_roundtrip.py`, `test_multi_edit.py`,
      `test_observe_memory.py`).

## 5. C5 — chore: cleanup

- [x] 5.1 `tasks.md` checkbox pass (this pass).
- [ ] 5.2 Linux CI is authoritative for the full suite, latency gate, and
      golden gate — this Windows box cannot run them
      (`fable-delegate-claude-only-windows` harness constraint).

## 6. Verification

- [x] 6.1 C1-C4 pinned suites stay green on Windows (see 2.5, 3.6, 4.4).
- [ ] 6.2 `uv run ruff check src tests` clean on changed files — ruff is not
      installed in this environment; the orchestrator runs it out-of-band.
- [ ] 6.3 Linux CI: full suite, latency + golden gates (authoritative; this
      box cannot run them).
- [ ] 6.4 Live smoke after merge + deploy: induce a `remember` under a
      slow/cold validator and confirm `exomem_boundary_hold_ms` and the
      mutation journal show a narrowed hold, not the pre-change multi-minute
      one.
