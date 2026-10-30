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
- [x] 5.2 Linux CI is authoritative for the full suite, latency gate, and
      golden gate — this Windows box cannot run them
      (`fable-delegate-claude-only-windows` harness constraint).
      Evidence: PR #327 (merge 4246bd8a3) merged with the full suite, golden gate and package checks green on Linux CI.

## 6. Verification

- [x] 6.1 C1-C4 pinned suites stay green on Windows (see 2.5, 3.6, 4.4).
- [x] 6.2 `uv run ruff check src tests` clean on changed files — ruff is not
      installed in this environment; the orchestrator runs it out-of-band.
      Evidence: PR #327 merged with the lint and targeted type check green; later PRs #1412, #1501, #1504 and #1901 report Ruff clean.
- [x] 6.3 Linux CI: full suite, latency + golden gates (authoritative; this
      box cannot run them).
      Evidence: PR #327 merged with the full suite, product end-to-end and golden gate checks green on Linux CI.
- [ ] 6.4 Live smoke after merge + deploy: induce a `remember` under a
      slow/cold validator and confirm `exomem_boundary_hold_ms` and the
      mutation journal show a narrowed hold, not the pre-change multi-minute
      one.

## 7. Availability repair: episode fanout and graph resolver preparation

- [x] 7.1 Add red-first episode contention regressions; preserve serialized first
      revision, supersession, identical retry, direct-call and fast-ack behavior.
      Evidence: PR #1412 (merge 0c29ca1c3): `test_blocked_episode_fanout_does_not_hold_the_next_writer` and `test_concurrent_initial_episode_records_keep_one_live_revision` in `tests/test_episode_memory_command.py`.
- [x] 7.2 Persist episode derived demand and defer fanout, heat and nudge until
      after the command guard and terminal persistence.
      Evidence: PR #1412: `test_episode_fanout_failure_keeps_a_committed_result_and_durable_demand` and `test_direct_episode_record_runs_fanout_with_durable_exact_paths` in `tests/test_episode_memory_command.py`.
- [x] 7.3 Prepare graph drain resolver outside the boundary; prove a concurrent
      checkpoint change leaves debt queued and does not publish stale topology.
      Evidence: PR #1412 and #1501: `test_cold_drain_resolver_does_not_hold_the_writer_boundary` and `test_drain_rejects_a_resolver_prepared_before_a_concurrent_write` in `tests/test_graph_drain.py`.
- [x] 7.4 Run affected suites, independent correctness review, public-artifact
      gate, strict OpenSpec validation and completion CI.
      Evidence: PR #1412 and #1501 merged with independent review, Ruff, the privacy gate, strict OpenSpec validation and green CI.
- [ ] 7.5 Stage a verified candidate before replacement; verify concurrent writes
      through the live service, both hostnames, receipts and boundary timings.
      Preserve the old service until the candidate is ready.

## 8. Fan-out after the creation namespace is released

- [x] 8.1 Red-first `tests/test_fanout_after_creation_lock.py`: a batch
      committed under `vault_creation_lock` fans out only after release, on the
      writer's context; outside any lock it still fans out inline; a body
      failure after commit still fans out and keeps its own error; an
      `observe_memory` commit completes its lexical component with no repair
      scheduled and no full-index receipt.
- [x] 8.2 `vault.py`: queue the post-commit fan-out on the held creation lock
      and run it at release in a copied context; stamp `derived.fanout` after
      the deferred run. `semantic_writes._commit_existing` attaches the
      post-release index report.
- [ ] 8.3 Live: after deploy, the personal service logs no
      `lexical sidecar upsert deferred (VAULT_LOCK_NESTED ...)` for
      `edit_memory`, `observe_memory` or `remember`, and `full_upserts` stops
      growing with ordinary writes.


## 9. Availability republication without full parsing under authority

This urgent availability repair ships independently of the memory-storage
batch: it addresses interactive write refusals while preserving graph proof
authority and can be rolled back independently.

- [x] 9.1 Prepare the source/topology proof outside the boundary and replay its
      exact byte/membership seal under authority; retain epoch, recall, policy,
      registry and live-sidecar binding and unchanged acknowledgement semantics.
      Evidence: PR #1501 (merge 6e00cc472): `test_a_slow_availability_proof_does_not_block_a_canonical_write` in `tests/test_graph_drain.py`.
- [x] 9.2 Verify an unrelated writer progresses during a slow proof and that
      intervening source, policy, registry or sidecar changes refuse publication.
      Evidence: PR #1501: the same test, and `test_availability_rejects_changes_after_its_source_proof` in `tests/test_graph_drain.py`.
- [x] 9.3 Measure remaining final-seal hold, run affected graph suites, independent
      review, privacy and strict specification checks and completion CI.
      Evidence: PR #1501 measured the final-seal hold on a synthetic 1,000-note vault, ran the graph suites, and merged after independent review, privacy, strict OpenSpec checks and green CI.
- [ ] 9.4 Deliver through the normal release process and verify the live personal
      service's write and graph outcomes before claiming the incident resolved.
- [x] 9.5 End an uncommitted final republication seal when a writer queues;
      retain exact publication proof and distinguish deferral from failed proof.
      Prove writer progress during source-byte and both membership checks,
      withheld availability before the writer commits, and no rebuild on deferral.
      Complete independent review, affected suites and ordinary source delivery.
      Keep the installed write and graph acceptance in 9.4 open until observed.
      Evidence: PR #2023 (merge eabd3a75a) passed independent review, the affected
      graph suite and exact-head CI. Its regressions cover source-byte sealing,
      both membership checks and retained repair debt after writer deferral.

## 10. Capture survives contention

- [x] 10.1 Red-first `tests/test_capture_boundary_hold.py`: a cold identity
      inventory is built while the boundary is free for `episode_memory` and a
      `record_memory` update; `reserved_paths.warm_identity_catalogue_before_boundary`
      called from `LeaseManager.invoke`; index page counts use one scandir pass.
- [x] 10.2 Red-first `tests/test_capture_contention_absorbed.py`: concurrent
      `remember`, `episode_memory` and `record_memory` append writes meeting a
      held boundary all commit once with no `MUTATION_BUSY`; an overdue holder
      and an exhausted wait refuse with a `cause`. `LeaseManager` waits in
      arrival order (`_FairCaptureQueue`) around `idempotency.run`.
- [ ] 10.3 Live: after deploy, `exomem_mutation_busy_total` stays flat under
      maximal capture and `contention_absorbed` log rows show the waits.
- [x] 10.4 Prepare managed episode reference owners outside the canonical guard;
      seal exact batch ownership and reload pending custody and policy under it.
      Share eligible preparation retries with existing capture allowance and waiter admission.
      Preserve direct calls, serialized revisions and both acknowledgement modes.
      Evidence: PR #1901 (merge 078653a35): `test_cold_episode_references_leave_the_boundary_free_for_another_writer` in `tests/test_capture_boundary_hold.py`.
- [x] 10.5 Prove another writer can commit during a cold owner scan, admission
      invalidation triggers fresh preparation, and concurrent captures survive repeated invalidation.
      Verify bounded admission and allowance exhaustion write nothing and permit same-identity recovery.
      Verify structured retryability through the command facade and run affected checks.
      Evidence: PR #1901: `test_reference_preparation_survives_two_real_intervening_commits`, `test_three_reference_captures_survive_concurrent_preparation_invalidation` and `test_unproved_episode_references_return_a_bounded_retryable_failure_without_a_write` in `tests/test_episode_memory_command.py`.
- [x] 10.6 Widen absorption from the capture commands to every keyed write:
      a guarded `record_memory` update meeting a held boundary waits and commits
      once (`test_a_guarded_record_update_waits_out_the_boundary`). Never re-run a
      narrow write that fetched caller handles
      (`test_a_busy_boundary_never_fetches_a_handle_twice_in_one_call`), and never
      start an attempt the request budget cannot finish, also after a wait in line
      (`test_a_slow_attempt_is_not_started_again_past_the_request_budget`,
      `test_a_wait_in_line_is_counted_before_a_slow_attempt_starts_again`).
      Declare every field a busy refusal carries in the REST `Error` schema.

## 11. Graph receipt identity after authority release

- [x] 11.1 Carry the dispatch checkpoint through the internal index report and
      both handoff/completion checks without exposing it in public dictionaries.
      Evidence: PR #1504 (merge b9870081c): `test_graph_completion_checks_its_dispatch_after_a_newer_epoch` and `test_a_missing_graph_dispatch_identity_cannot_borrow_the_global_checkpoint` in `tests/test_index_sync.py`.
- [x] 11.2 Reproduce a real sibling commit after completed dispatch; verify
      registration and queued coverage survive a newer epoch, while missing
      identities, missing flights and stale queue generations still refuse.
      Evidence: PR #1504: `test_completed_batch_keeps_its_graph_receipt_when_a_sibling_commits` in `tests/test_graph_fanout_totality.py`.
- [x] 11.3 Run affected suites, independent review, privacy, strict OpenSpec
      validation and completion CI; deliver after the frozen 0.102 release.
      Evidence: PR #1504 merged after independent review, privacy, strict OpenSpec checks and green CI.
- [ ] 11.4 Verify live committed receipts and graph convergence after managed
      upgrade before closing the graph incident or friend-readiness hold.

## 12. Curation proposals prepare outside the boundary

- [x] 12.1 Narrow a curation proposal to the writer fence in `LeaseManager.invoke`
      and make `curation.seal_proposal` take the mutation boundary, verify every
      binding under it and refuse a stale plan before it writes. Capture registry
      identities at the start of `prepare_proposal`. Apply, resume and
      compensation stay wide.
- [x] 12.2 Red-first
      `test_curation_proposal_preparation_leaves_the_boundary_free_and_the_seal_rechecks`
      in `tests/test_shorten_critical_section.py`: a real proposal parked after
      preparation does not block a real `edit_memory` on an unrelated page or on
      the bound page; the unrelated case seals under a held boundary, and the
      bound case fails with `CURATION_BINDING_STALE` and creates no run.
- [ ] 12.3 Live: after deploy, an installed curation proposal on a large vault
      no longer makes concurrent governed writes wait or fail with
      `MUTATION_BUSY`.
