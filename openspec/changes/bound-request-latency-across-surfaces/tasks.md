# Tasks

## 5. Ordinary write follow-through

- [x] 5.1 Serve page-scoped write advice with independent emission history and delivered fingerprints; verify global carriers, disclosure, and batch behavior.
      Evidence: PR #1838: `test_write_advice_counts_only_affected_pages`, `test_local_delivery_does_not_reset_global_emission_history`, `test_batch_advice_counts_union_of_affected_pages` and `test_delivery_records_the_fingerprint_that_was_shown` in `tests/test_due_state_projection.py`; `test_local_write_advice_remains_independent_of_bootstrap_counts` in `tests/test_due_state_carriers.py`. Disclosure, from PR #1700 (merge 34c1a76a4): the bounded case `test_the_served_view_is_byte_identical_to_a_vault_without_the_item[True]` and `test_bounded_due_reads_follow_the_current_principal_and_release_policy` in `tests/test_due_state_projection.py`.
- [x] 5.2 Publish receipt proof under the existing outermost vault hold; verify overlapping multi-hold supersession, direct fallback, degradation, and replay.
      Evidence: merged PR #1051 added `test_governed_write_publishes_pending_visibility_through_lane2` and `test_immediate_write_is_visible_by_stable_ref_keyword_and_hybrid` in `tests/test_fast_write_integration.py`. Their unguarded narrow-command leaves exercise the real receipt store and direct fallback publisher. The same module covers overlapping holds with `test_superseded_batch_releases_the_session_advisory_claim` and replay with `test_exact_replay_returns_the_original_terminal`. Degradation is covered by `test_real_custody_failure_degrades_the_persisted_terminal_to_pending` in `tests/test_fast_write_ack.py` and `test_lost_derived_acknowledgement_persists_a_pending_success_terminal` in `tests/test_writer_lease.py`.
      Not claimed: the interleaving where fallback starts with live custody and canonical bytes change after proof releases its guard. The fallback still forces publication; the named tests do not verify that narrower race.
- [x] 5.3 Prime the writer resolver from the proven structural corpus; verify freshness, recall separation, and blocked-edit behavior.
      Evidence: `tests/test_proportional_writer_resolver.py` covers startup priming, freshness refusal and recall separation. Priming changes only the broad resolver cache and its checkpoints. `edit.edit` calls `load_editable` before requesting a resolver; that guard refuses Sources/Evidence and checks current bytes for reserved or superseded pages. Existing refusal tests include `test_edit_still_refuses_sources` and `test_edit_still_refuses_superseded` in `tests/test_get_edit_roundtrip.py`, plus `test_edit_refuses_private_hardlink_before_parse_or_staging` in `tests/test_reserved_admin_paths.py`. These independent boundaries establish blocked-edit behavior without another test that combines priming and refusal.
- [x] 5.4 Refresh manifest claims after revisions and add CPU attribution to existing spans; verify changed claims and unchanged release checks.
      Evidence: PR #1838: `test_manifest_revision_refreshes_routing_before_projection_ttl` in `tests/test_collection_claims.py` and `test_store_manifest_changes_refresh_the_shared_claims_projection` in `tests/test_collection_store_writer.py`; CPU spans in `tests/test_call_spans.py`.
- [ ] 5.5 Reuse installed acceptance tools for matched warm writes, all outcomes, and honest coverage; independently verify latency, CPU, custody, and convergence.
- [ ] 5.6 Package one shared read-only MCP canary for `ask_memory`, `activate_context` and `read_memory`; preserve the existing script interface and verify installed availability, acceptance and incomplete evidence. Add strict schema expectations beside unchanged indexed expectations. Verify source-bound membership, optional rank, private validation, local references and incomplete observer failures. Retain safe validator path fingerprints for mismatch correlation without changing outcomes or claiming unique assertion identity. Require independent source-only key checks and new series without changing frozen keys or exposing unused cases.
- [ ] 5.7 Install the availability canary through systemd user units and expose offline status; verify credentials, actual execution and authorized notification delivery. Verify release acceptance across active read and write streams.

## 1. Prove the source and public boundaries

- [ ] 1.1 Reconcile final integrated source with current contracts; verify incoming connector-boundary checks and existing acknowledgement custody before product edits.
- [ ] 1.2 Prove input, stored-row and returned-result counts and sampled identities on isolated reference state; record discrepancies before deriving latency.
- [ ] 1.3 Capture installed baseline intervals for MCP, REST and activation leaves; verify each declared boundary includes its named work.
- [ ] 1.4 Record profiles, populations, windows, coverage, CPU caps and RSS; verify model-free and live observations remain separate.

## 2. Centralize bars and extend existing observations

- [ ] 2.1 Add branch tests for pending bars, invalid readings, population mismatch, retention loss and mixed outcomes. Verify false passes fail first. Include 90 fast successes and 10 errors above the p95 bar; verify the all-request result fails despite healthy success-only timings.
- [ ] 2.2 Package `request_latency_contract.json` and load it in existing consumers. Verify thresholds and outcome-inclusion rules resolve once with owning requirements and provenance.
- [ ] 2.3 Extend `latency_watch` counts and summaries; verify unknown, partial, insufficient and evicted observations cannot establish complete-population acceptance.
- [ ] 2.4 Wire MCP middleware, REST facade and activation observations; verify one sample per boundary, including refusal/error and nested activation cases.
- [ ] 2.5 Update existing operator documentation; verify bootstrap isolation, doctor coverage and structured log output through real isolated calls.
- [ ] 2.6 Project the nine-field caller-local advisory for compact and session bootstrap; retain full spans and coverage for full and diagnostics. Correct populated spans and verify unchanged ceilings on every maintained surface.

## 3. Complete existing gates and repair attributed costs

- [ ] 3.1 Complete `accelerate-governed-recall` task 6.1 through its existing gate; verify its owning reference, boundaries and source-loaded bars.
- [ ] 3.2 Attribute remaining activation walks, matrix reloads, due-state and authorization costs; verify current spans and CPU support each proposed repair.
- [ ] 3.3 Repair only attributed costs through existing incremental paths; verify unchanged results, packet content, authorization and pending-read visibility.
- [ ] 3.4 Select residency from measured RSS and cold/warm costs; verify encoder output identity, CPU-cap compliance and documented rollback.
- [ ] 3.5 Localize overdue activation intervals through a bounded isolated reproduction; record the responsible work, including packet finalization. Reuse `RequestBudget` checks in controllable loops/waits and existing safe deferral paths; retain managed index rebuild deferral. Verify packet or existing budget abstention returns within the absolute deadline; classify every overrun as failed and unresolved. Preserve canonical mutation immunity and exact terminal/component custody; keep an unsafe-to-bound repair open while independent work continues.
- [ ] 3.6 Establish pending bars from current measurements where justified; verify recorded choices and keep remaining thresholds null and pending.
- [x] 3.7 Keep optional direct-read links off the foreground build path. Verify cold and evicted reads release permitted bodies with unknown inbound links, existing watcher recovery restores exact links, and mutation safety remains complete.
  - Evidence: merged in #1880 (12cde6096) with the cold and evicted direct-read tests.
- [x] 3.8 Publish the free-text artifact census as a base and an add-only overlay (design decision 9).
  - Add aliases from self-write registration and from watcher events, before their Markdown filters.
  - Rebuild the base in the background from event-named directories, plus a periodic walk.
  - Read the base and overlay without enumeration. Check path-shaped unresolved tokens once.
  - Keep the synchronous walk while unpublished. Cache tombstone aliases by lifecycle generation.
  - Verify: a service-written withheld artifact with the owner stopped; an external create after its event; a path-shaped token; removed-name shadowing; the unpublished fallback; no request-thread enumeration.
  - Evidence: merged in #2068 (21985c24a); tests/test_artifact_census_publication.py.
- [x] 3.9 Cache the admitted recall path set per admitted checkpoint, filled by catalogue publication. Shadow pending paths by set difference.
  - Verify: identity with a fresh selection at the same checkpoint; a cache miss on a policy change; pending-overlay visibility.
  - Evidence: merged in #2068 (21985c24a); tests/test_admitted_recall_path_set.py.
- [x] 3.10 Catch up the embedding matrix on the derived drain after a batch commits.
  - A request serves a lagging matrix only when its epoch, instance, space and policy match. It starts the single-flight catch-up in the background.
  - Verify: a stale serve never returns a changed path's text; own writes stay visible; a policy-identity change still refuses; the request thread runs no catch-up.
  - Evidence: merged in #2068 (21985c24a); the catch-up runs on the background matrix loader, not the derived drain; tests/test_embedding_matrix_lag_serve.py.
- [x] 3.11 On a due-state key miss, serve the newest build for the same vault, audience, resolution and purpose, after the existing re-checks. Schedule the refresh for the new key.
  - Verify: dismissals and removals apply at once; no reuse across audiences; served content equals a fresh build after the refresh.
  - Evidence: merged in #2068 (21985c24a); tests/test_due_state_stale_reuse.py.
- [x] 3.12 Use the non-building inbound lookup in context-pack neighbourhoods. On a miss, mark inbound neighbours incomplete and record demand.
  - When a patch bridge is incomplete, republish the patched index instead of emptying the cache.
  - Keep a reverse map from file to targets, so that a patch costs the change.
  - Verify: a self-written link appears without a rebuild; the explicit inbound command keeps its synchronous lookup; the patched index equals a full rebuild.
  - Evidence: merged in #2068 (21985c24a); a miss starts one background build instead of recording demand; tests/test_pack_inbound_recovery.py.
- [x] 3.13 Resolve references from the identity projection while it is stale. Keep owners under the knowledge-base trash folder in a table used only for references, outside the uniqueness checks.
  - Verify: a trash-owned reference resolves with the census scan disabled; the path decision stays live.
  - Evidence: merged in #2068 (21985c24a); tests/test_memory_refs.py.
- [x] 3.14 Keep proven pending rows from publication by path, after-hash, mtime and size, so that a rehydrate re-reads only changed bodies.
  - Verify: pending edits and deletes still hide stale rows.
  - Evidence: merged in #2068 (21985c24a); tests/test_pending_recall_delta.py.
- [ ] 3.17 Compute coverage flags relative to the caller's view: the activation `index_stale` and `lexical_evidence`, the direct-read `inbound: null`, and the context-pack inbound and outbound incomplete entries.
  - For a restricted caller, report a lag only when a changed path since the published generation is one the caller may see. Use the freshness delta and the existing release decision. The cost follows the lag, not the vault.
  - Verify: an owner write to a withheld page leaves a restricted caller's response unchanged; a write to a visible page is reported; owner responses are unchanged.
  - Still open in the same class:
    - The activation `lexical_evidence` follows the lexical store's readiness proof, which gates the served evidence, so a hidden write can still read as "stale" until the lexical catalogue catches up.
    - `context_catalogue.ensure_unlisted` folds the vault-wide `index_stale` into `unlisted`.
    - A project-registry change reports stale to every caller.
    - A deleted or moved withheld page counts as visible.
    - A lag longer than the decision bound (16 paths), or an unbridgeable change history, reports the lag without deciding each path, so a burst of withheld writes can still flip a restricted caller's flag until the structure is published.
- [ ] 3.15 Publish admitted sets for structured eligibility and the activation carry. Key them by audience, purpose, grants, tombstone generation and catalogue generation. Patch them from catalogue deltas, and abstain on a key mismatch.
  - Verify: a revoked or retagged page leaves the next request's eligibility and rarity.
  - This is a separate slice from 3.8-3.14.
- [ ] 3.16 After installation, compare `ask_memory`, `activate_context` and `read_memory` dispatch timings in the request ledger by concurrency bucket: alone, 1.5 to 3, and 3 to 6.
  - Use at least 100 calls per tool per arm, report the write count per window, and give bootstrap 95% intervals.
  - Record each site's span check from 3.8-3.14.

## 4. Verify installed behavior and deliver

- [ ] 4.1 Compare isolated installed arms on identical inputs; verify per-case confidence intervals, CPU, memory, data, quality, freshness and packet identity.
- [ ] 4.2 Run the complete project corpus, lint, build, privacy and strict OpenSpec checks; record fresh output against delivered bytes.
- [ ] 4.3 Obtain independent review of the exact integrated batch; resolve findings and verify each correction through a scoped recheck.
- [ ] 4.4 Release and install through existing ownership and gates; verify live latency and custody read-only without enabling a flag.
- [ ] 4.5 Record any actual switch's preconditions, owner, undo and post-switch evidence; verify rollback preserves existing terminals and receipts.
- [ ] 4.6 Close only proven requirements after merge evidence; sync/archive this change through OpenSpec and verify strict validation before and after.

Product work uses temporary runtime roots and an isolated tenant with external effects blocked. Existing acknowledgement acceptance and rollout tasks keep their owner. Pending bars do not invent defaults or third-party release holds. Metered evaluation follows the existing budget agreement; these artifacts authorize no provider run or held-out-key construction.

## Source-loaded observation slice: verification scope

The implementation packages the bar source and extends the existing watch,
MCP dispatch, REST command, activation log, bootstrap and doctor surfaces.
It retains the existing request-budget limits and canonical-write immunity. Activation collects existing
named stages without exposing public timing payloads unless the caller opts in.
The runtime observation profile remains pending because automatic observations
cannot prove pre-call warmth or installed acceptance.

The isolated model-free workflow tests prove separate dispatch and leaf samples,
all terminal outcomes, response construction, finalization errors, caller isolation
and partial source coverage. The initial contract branch run had 11 failures.
The scoped observation run passed 196 tests. The adjacent activation, conversation
and timing run passed 126 tests. These runs use Python 3.13.12 and disabled
embedding, CLIP and warm-up lanes; they establish no served-model latency gain.

The wheel and source distribution build with the pinned uv writer. The built
wheel loads the contract through `importlib.resources`. The public-artifact gate,
strict OpenSpec validation and harness-module generator checks pass.

These checks do not complete installed baseline or live acceptance tasks.
CPU caps, RSS, current warm residency, paired latency gains and deadline repair
remain unproved. Task boxes remain open until integrated delivery supplies the
required evidence. The foreground and durable-acknowledgement owners retain
their unfinished acceptance work and default-off fast acknowledgement.

## Attributed graph wait and budget custody

An isolated reproduction found four diagnostic queue reads after graph unavailability.
Activation discards those lag values, so it now omits that diagnostic work.
Public graph diagnostics retain their existing default behavior.
The graph expansion loop checks the existing budget between controllable steps.
Finalization and logging now retain the same activation budget until the leaf exits.

A real exclusive SQLite queue test returns before the remaining absolute deadline.
Its nine packet blocks match the equivalent unavailable-graph reference.
The final integrated scope passed 90 activation, graph, logging and watch tests.
The existing finalization-error workflow also verifies budget custody and restoration.

This bounded repair does not localize the installed service's recorded overruns.
Other I/O, graph snapshot proof and publication waits remain unbounded.
Task 3.5 stays open; neither a live gain nor full deadline acceptance is claimed.

## Bounded retrieval carry

Attribution on an installed cell placed most abstaining activation time in the
retrieval carry. The carry now stops each rarity count one page past its
decision window. An owner whose configuration can withhold no page ranks on
the catalogue's statistics, and corroboration stops once it holds its rows
(design decision 7). New ledger
spans name each carry rung and each role lane's catalogue and hydration work.
A `runtime.gc` span records collector pauses.

A new model-free gate times an abstaining turn's carry right after a catalogue
write, at 2,000 and 8,000 notes, on Python 3.12.3 with SQLite 3.45.1 as in CI.
On 0.121.0 the median carry grew from 118 ms to 607 ms (5.2x), and the gate fails.
With this change it grew from 40 ms to 93 ms (2.4x), and the gate passes.
Identity tests show that bounded rarity and rank-order corroboration give the
same answers as before. Twin tests delete a vault's only withheld page. For a
restricted reader, and for the owner under a governance policy, both carry
rungs return the same rows and scores with the page present or deleted.

The installed measurement is still open. Tasks 3.2 and 3.3 stay open until the
cell reports the new carry spans on at least fifty abstaining carries.
