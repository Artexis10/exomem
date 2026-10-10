# Tasks

Delivery A is the current implementation batch. Deliveries B and C remain required for T16 and precede real managed-vault consolidation. All checks use temporary state unless an operator authorizes a named live operation.

Evidence for the checked Delivery A tasks. Each test passed in a scoped local run on head 9cbfe771a, and the pull request's CI runs the full corpus. Delivery A lands as commit 168922522 on base 8365f0e92 (#1614). The branch later merged main at 7b242bfa8 and also carries Delivery B. Commit dfa5870c3 followed and added two move tests and one find-consumer test. They passed with the first group's files (48 tests) and count under 3.5 and 4.3. Tests that run the actual older runtime need an exomem 0.106.0 environment in `EXOMEM_TEST_OLDER_READER_PYTHON`, as CI installs it.

Scoped runs, each `python -m pytest -q -p no:cacheprovider` over the files named:
- `tests/test_connector_principal.py`, `tests/test_connector_boundary.py`, `tests/test_connector_boundary_arming.py` and `tests/test_connector_boundary_e2e.py`: 45 passed.
- `tests/test_connector_boundary_portability.py` and `tests/test_connector_boundary_restore_v4.py`: 39 passed.
- `tests/test_governance_membership.py`, `tests/test_hosted_portability.py`, `tests/test_hosted_restore_candidate.py`, `tests/test_upload_tokens.py`, `tests/test_download_token_principal.py`, `tests/test_governance_principal.py` and the named collection-store, source-kind and receipt tests: 306 passed, 1 skipped (needs root).
- `tests/test_governance_egress.py`, `tests/test_governance_postfilter.py` and `tests/test_governance_companion_backfill.py`: 447 passed with `--timeout=300`.
- `tests/test_derived_identifier_egress.py` was not run in full. Its restricted-move tests take about 60 s each on this head and on main, which reaches the default 60 s timeout. `test_a_restricted_move_judges_no_page_only_a_withheld_relation_reaches` passed its four cases with `--timeout=300`.
- `tests/test_context_activation_real_compiler.py`, `tests/test_compile_proposal.py`, `tests/test_working_set_currency_security.py`, `tests/test_working_set_temporal_currency.py` and `tests/test_activation_conventions_compiler.py`: 236 passed and 2 failed, named under 4.7. Both pass on main 5c38c1f2b.

Evidence by task:
- 1.1 `openspec validate --all --strict` with the pinned 1.14.0 CLI passed 239 items with 0 failed. The retained-worth rows of design.md section 9 map to the `vault-consolidation` requirements from "Offline inventory accounts for every canonical object" to "Real import and cutover require operational authority", with `disclosure-evidence` for receipts.
- 1.2 Base 8365f0e92 contains `governance/raw_protection.py` and `governance/companions.py`, and `raw_protection.py` is unchanged through this head. Authentication propagation is proven under 2.1 and 2.2, and registry behavior under 4.5. The scoped proof commands are the runs above.
- 1.3 `tests/test_connector_boundary.py::test_same_owner_clients_have_different_nonbypassable_content` fails on base 8365f0e92 at `assert not egress.quick_page_visible(vault, PRIVATE, principal=limited)`: the limited client reads the protected page. It passes on this head. The `configured_boundary` fixture authenticates through a real `SessionAuthority` and serves six other test files. `tests/test_connector_boundary_e2e.py::test_oauth_owner_twins_keep_read_search_and_browse_independent_of_hidden_content` builds the two-vault twin.
- 2.1 `tests/test_connector_principal.py::test_verified_client_and_login_survive_owner_normalization_and_transfer`, `::test_arbitrary_verified_claims_cannot_create_a_connector_binding`, `::test_local_grant_retains_client_and_login_on_mcp_and_rest`.
- 2.2 `tests/test_connector_principal.py::test_verified_client_and_login_survive_owner_normalization_and_transfer`, `::test_delegated_download_stops_when_origin_login_ends` (revoke, generation, expiry, refresh-family).
- 2.3 `tests/test_connector_boundary.py::test_shared_and_legacy_owner_ingress_gets_restricted_default` (rest, transfer), `::test_unknown_and_hosted_clients_cannot_borrow_configured_owner_access`, `::test_explicit_local_administration_does_not_travel_in_a_download_capability`. A legacy `v2.` owner capability and the shared bearer both resolve to `owner_principal(surface="transfer")` in `download_principal`. A scratch probe, not committed, confirmed both read a protected page as absent through `/download` on an armed vault.
- 3.1 `tests/test_connector_boundary.py::test_unknown_scope_never_turns_configuration_into_unrestricted_access`, `::test_malformed_host_configuration_is_an_unavailable_boundary` (duplicate-key, unknown-field, empty-default, wider-default), `tests/test_connector_boundary_arming.py::test_arming_requires_offline_authority_and_missing_configuration_cannot_disarm`.
- 3.3 `tests/test_governance_membership.py::test_canonical_reference_controls_markdown_membership` (included, excluded), `::test_malformed_identity_cannot_escape_canonical_reference_membership`, `::test_legacy_identity_does_not_change_nonidentity_membership`, `::test_ref_selector_resolves_membership`.
- 4.5 `tests/test_connector_boundary.py::test_private_registries_cannot_change_resolution_bootstrap_or_save_outcomes`, `::test_limited_owner_can_update_a_wholly_admitted_public_relation_registry`, `::test_private_registry_blocks_vocabulary_currency_before_work_item_lookup`.
- 4.6 `tests/test_connector_boundary.py::test_live_configuration_change_restricts_an_existing_filter`, `::test_rest_discards_a_result_when_configuration_changes_during_computation`, `::test_mcp_content_is_withheld_when_origin_session_ends_during_read` (resource, prompt), `::test_unknown_and_hosted_clients_cannot_borrow_configured_owner_access` (a re-registered client id), `tests/test_collection_store_governance.py::test_owner_connector_ceiling_filters_canonical_rows_and_live_summary_cache`, `tests/test_connector_principal.py::test_delegated_download_stops_when_origin_login_ends`.
- 4.8 `tests/test_source_kind_required.py::test_owner_connector_source_counts_and_capture_debt_ignore_hidden_sources`.
- 5.1 `tests/test_connector_boundary_arming.py::test_arming_requires_offline_authority_and_missing_configuration_cannot_disarm`, `::test_interrupted_arming_cannot_serve_and_exact_retry_repairs_publication`, `::test_maintenance_command_arms_the_configured_boundary`.
- 5.2 `tests/test_connector_boundary_arming.py::test_arming_preserves_active_selectors_and_detects_same_id_reclassification`, `::test_arming_requires_offline_authority_and_missing_configuration_cannot_disarm`, `tests/test_connector_boundary.py::test_unknown_scope_never_turns_configuration_into_unrestricted_access`, `tests/test_connector_boundary_portability.py::test_inconsistent_armed_archives_refuse_before_restore_staging` (selectors).
- 5.3 `tests/test_connector_boundary_portability.py::test_armed_export_carries_protection_and_actual_v1_reader_refuses` (version 2, and the exomem 0.106.0 verifier refuses with `UNSUPPORTED_MANIFEST_VERSION`), `tests/test_hosted_portability.py::test_repeat_export_is_deterministic_complete_and_excludes_runtime_state` (unarmed version 1), `tests/test_connector_boundary_portability.py::test_inconsistent_armed_archives_refuse_before_restore_staging` (selectors, missing-artifact, version-downgrade).
- 5.4 `tests/test_connector_boundary_portability.py::test_restore_installs_protection_without_source_clients_and_waits_for_destination_config`, `::test_protective_scope_restore_refuses_existing_selector_conflict`, `::test_hosted_restore_recovers_its_protected_publication`.
- 5.5 `tests/test_connector_boundary_portability.py::test_actual_older_runtime_accepts_unarmed_root_and_refuses_after_enrollment` (exomem 0.106.0, unpatched).
- 5.7 `tests/test_connector_boundary.py::test_unknown_and_hosted_clients_cannot_borrow_configured_owner_access`, `::test_shared_and_legacy_owner_ingress_gets_restricted_default`. `raw_protection.py` is unchanged from base 8365f0e92 to this head, so the hosted RAW exemption is not extended. The gateway dependency is in proposal.md Impact and design.md section 8.
- 5.8 `tests/test_connector_boundary_portability.py::test_interrupted_protection_restore_resumes_original_proposal` (7 boundaries; one proposal exists after resume), `::test_hosted_restore_recovers_its_protected_publication`, and the hosted journal regression in `tests/test_hosted_restore_candidate.py`.
- 5.9 `tests/test_connector_boundary_portability.py::test_interrupted_protection_restore_resumes_original_proposal`, `::test_restore_retry_refuses_unbound_residue_without_discarding_it` (scope, registry, unrelated, receipt, missing_head, foreign_chain, operation, archive, destination, placement), `tests/test_governance_receipts.py::test_staged_evidence_requires_its_destination_authority`.
- 5.10 `tests/test_connector_boundary_restore_v4.py::test_v4_full_restore_resumes_same_proposal` (the spent proposal carries `restore-continuity/v1`; every manifest file is byte-equal), `tests/test_connector_boundary_portability.py::test_hosted_restore_recovers_its_protected_publication`, `::test_restore_retry_refuses_unbound_residue_without_discarding_it`.
- 5.11 `tests/test_connector_boundary_restore_v4.py::test_v4_full_restore_resumes_same_proposal` (schema 4 enrolled; later_scope, missing_custody, changed_custody, substituted_inode, competing_tree), `tests/test_connector_boundary_portability.py::test_interrupted_protection_restore_resumes_original_proposal`, `::test_hosted_restore_recovers_its_protected_publication`, `::test_restore_preserves_published_tree_for_exact_crash_recovery` (process_death, protection_failure), `::test_restore_retry_refuses_unbound_residue_without_discarding_it`, `::test_protected_restore_resumes_existing_physical_migration`.

Open: 3.2, 3.4, 3.5, 3.6, 4.1, 4.2, 4.3, 4.4, 4.7, 5.6 and 6.1 are implemented with the proof gap named under each task. 6.2 is partly recorded. 6.3 and 6.4 belong to the delivery owner. Tasks 4.9 to 4.15 are not assessed here.

## 1. Reconcile the contract and establish the proof boundary

- [x] 1.1 Reconcile the proposal, design, and all capability deltas; verify strict OpenSpec validation and preserve every unique import guarantee.
- [x] 1.2 Confirm merged RAW and compiler seams, current registry behavior, and authentication propagation; record the exact implementation base and scoped proof commands.
- [x] 1.3 Create one reusable authenticated twin-vault fixture; demonstrate the owner-client bypass before changing branching admission logic.

## 2. Delivery A: verified connector and originating authentication bindings

- [x] 2.1 Add frozen bearer-free client and origin-session bindings; prove OAuth/local ingress retains verified facts through owner normalization and governance sessions.
- [x] 2.2 Preserve bindings through signed v3 transfers and reconstruct nested values; prove revocation, generation, expiry, and refresh-family changes invalidate consumption.
- [x] 2.3 Restrict legacy v2 transfers and unknown/shared-bearer ingress when armed; prove only explicit CLI, stdio, and library administration remains unrestricted.

## 3. Delivery A: configured admission and allowed writes

- [x] 3.1 Add host-owned JSON configuration referencing canonical Scope IDs; prove missing, malformed, and unknown references cannot disable an armed ceiling.
- [ ] 3.2 Meet ceiling decisions with existing membership decisions before owner/empty-policy shortcuts; prove grants, purpose, bridges, and exact releases cannot widen it.
  Gap: no committed test gives a limited owner a grant, purpose, bridge or exact release and shows the ceiling holds. A scratch probe, not committed, showed an owner-audience standing grant and a purpose do not widen it. Bridges and exact releases are unprobed.
- [x] 3.3 Repair Markdown canonical-reference membership using parsed valid identity; prove inclusion, exclusion, malformed-identity handling, and retained path-reference behavior.
- [ ] 3.4 Protect configuration and protective Scope definitions; configure capture namespaces and prove allowed capture/edit plus identical hidden-present and absent creation refusals.
  Gap: tests prove allowed capture and edit and the identical refusals. No committed test shows a limited connector cannot write a protective Scope document through the file writer (`connector_boundary.require_write`). Only the `govern_memory` surface is tested; a scratch probe, not committed, confirmed the writer refuses the write.
- [ ] 3.5 Enforce capture namespace visibility across all writers, moves, replacement, reclassification, imports/restores, and Scope/configuration changes; check old and proposed membership.
  Gap: tests cover unrestricted writers, folder aliases, sidecar reclassification, proposed membership, moves (`tests/test_connector_boundary.py::test_a_limited_move_answers_the_same_whether_or_not_a_hidden_page_holds_or_links_the_name`, `::test_no_writer_moves_protected_content_into_a_capture_namespace`), arming and Scope changes at startup. No test covers a replacement, a restore or import into a populated capture folder, or a `capture_paths` change.
- [ ] 3.6 Document configuration, restricted defaults, and explicit administrator ingress; exercise the documented temporary setup without deploying it.
  Gap: `docs/connector-boundaries.md` covers configuration and restricted defaults. It does not state that explicit CLI, stdio and library ingress stays unrestricted. No test runs the document's own example; `test_maintenance_command_arms_the_configured_boundary` runs the same command on fixture configuration.

## 4. Delivery A: pre-computation admission and live consumption

- [ ] 4.1 Cover shared egress, annotations, path-only records, release walks, fast paths, media, resources, and transfers; verify the twin fixture preserves allowed utility.
  Gap: no connector-ceiling test covers media frames (`release_allows_frames`). The other listed producers have tests: OAuth twin, release walk, resources, path-only artifact capture and downloads.
- [ ] 4.2 Admit collection rows before values decoding, inspection seals, and summaries; prove hidden rows cannot alter counts, hashes, errors, or cache results.
  Gap: tests cover hidden-row counts, snapshots, guards, audit status and the live summary cache. No test shows that a hidden row cannot change an error, or that its values are never decoded.
- [ ] 4.3 Filter retrieval, graph, context, working-set, canonical-state, continuity, history, alias, and citation contributors before derivation; verify paired observations.
  Gap: the OAuth twin test covers retrieval, graph and history reads, and `tests/test_private_vocabulary_instances.py::test_a_hidden_page_with_a_private_status_never_changes_a_limited_clients_find_consumers` covers evolution, context and link suggestions. No ceiling test covers working sets, canonical state, continuity, aliases or citations. FTS5 `bm25()` statistics still count hidden pages (7.8).
- [ ] 4.4 Restrict bootstrap, whole-corpus aggregates, registries, provenance, and receipts to admitted inputs or existing unavailable outcomes; prove hidden canaries cannot affect responses.
  Gap: tests cover bootstrap, registries, whole-corpus aggregates and adoption resources. No ceiling test covers provenance or receipts.
- [x] 4.5 Audit global registry uniqueness, aliases, folders, and hashes; preserve public-independent owner writes and prove hidden-dependent operations remain unavailable until Delivery B.
- [x] 4.6 Bind release and summary caches to client/configuration revision; prove live configuration changes, re-registration, and origin-session invalidation take effect at consumption.
- [ ] 4.7 Preserve compiler classification reuse and floor checks; run affected RAW/compiler regressions and prove canonical-state expansion remains useful.
  Gap: companion reuse and both floors are tested (`test_atomic_artifact_capture_uses_proposed_companion_and_cannot_be_reclassified_private`). At head 9cbfe771a the RAW and compiler regression run has two failures that pass on main: `tests/test_context_activation_real_compiler.py::test_the_recorded_report_is_the_current_product_run` (stale recorded report) and `tests/test_working_set_currency_security.py::test_withheld_reverse_supersession_does_not_change_lane_or_hook`. No test runs canonical-state expansion under an armed limited connector.
- [x] 4.8 Integrate the merged source-kind retirement before final security review; prove classification-debt advisories and kind-refusal counts honor the content ceiling.
- [ ] 4.9 Integrate the actual merged S1 successor before point-write implementation; reuse its fresh complete-field mutation obligation without changing RAW authority semantics.
- [ ] 4.10 Adapt admitted existing-row writes and unchanged validator inputs; prove target concurrency, before/after admission, held-candidate isolation, and private whole-container projection.
- [ ] 4.11 Add the post-S1 nullable unique scoped retry column and server-only outer point domain; preserve legacy ledger bytes without fallback lookups.
- [ ] 4.12 Admit the current target and complete fields before both retry lookups; require current v2 guards only on misses before any effect.
- [ ] 4.13 Project live and replay receipts through one historical envelope owner; prove current authority without reconstructing historical guards or repeating committed mutations.
- [ ] 4.14 Prove authenticated point-write twins, refresh and authority changes, legacy retry transitions, and the new migration's downgrade, restore, rollback, and query-plan behavior.
- [ ] 4.15 Separate verified clients in the shared explicit/implicit mutation retry owner; prevent transfer capability caching and prove reauthentication, cold caches, and legacy transition behavior.

## 5. Delivery A: durable arming and supported portability

- [x] 5.1 Arm only under stopped/drained maintenance using existing manifest locking and durable publication; prove crash after enrollment prevents service admission.
- [x] 5.2 Publish and classify the portable armed requirement with active protective Scope definitions and normalized selector fingerprints; prove missing configuration, environment, or referenced Scope cannot restore unarmed behavior.
- [x] 5.3 Export armed manifest version 2 and retain unarmed version 1; prove an actual older verifier rejects version 2 before extraction.
- [x] 5.4 Restore into fresh external state through destination canonical Scope/publication owners; refuse same-ID selector conflicts and prove compatibility survives without source authority.
- [x] 5.5 Run the actual older runtime against an armed root; record refusal without patching its capability catalogue or deleting the guard.
- [ ] 5.6 Document maintenance arming, restore configuration, and rollback to isolation; verify examples against temporary state and preserve guard requirements.
  Gap: `docs/connector-boundaries.md` documents arming and restore configuration. It does not document rollback to isolation.
- [x] 5.7 Record shared-cell provenance limits and the gateway dependency; prove armed hosted requests use the restricted default without extending the RAW exemption.
- [x] 5.8 Share hosted journal mechanics with standalone restore; reserve the exact proposal before rename and persist missing documents before issuance.
- [x] 5.9 Resume the original Scope proposal before policy loading; verify Scope, receipt, and pending-marker residue through canonical owners across separate evidence and authority roots.
- [x] 5.10 Share versioned complete-continuity evidence across protective proposal creation, commit, and recovery; retain membership for admission and prove full scaffold preservation and tamper refusal.
- [x] 5.11 Exercise legacy and enrolled v4 destinations, hosted recovery, process death, preserved destination inodes, changed custody and identities, tampered mirrors, invented events, and missing destination heads.

## 6. Delivery A: integrated acceptance and release

- [ ] 6.1 Run real OAuth/local twin-vault workflows across affected observations and writes; verify useful allowed results and hidden-corpus independence.
  Gap: the OAuth twin test covers read, search, browse and download. No twin workflow runs writes through a real transport or runs the local ingress. Writes are tested in-process with authenticated principals.
- [ ] 6.2 Run scoped regressions, lint, build, source generators, and privacy checks; record exact commands, logs, and product-versus-proof diff weight.
  Open: the privacy gate (`scripts/validate-public-artifacts.py --repository`) passes on this head. Lint, build, source-generator checks and the command and log record are not captured. Delivery A commit 168922522 weighs +1960 and -336 product lines in 44 files, against +1571 test lines in 12 files.
- [ ] 6.3 Obtain independent adversarial security review of the exact batch; resolve findings and obtain correction-scoped rechecks with a plain ship decision.
- [ ] 6.4 Run the full corpus in completion CI and deliver through ordinary merge/release; record evidence and leave Deliveries B and C open.

## 7. Delivery B: private vocabulary domains after the registry foundation

Evidence for the checked Delivery B tasks. Each test passed in a scoped local run on the delivery branch, and the pull request's CI runs the full corpus.

- 7.1 `tests/test_private_vocabulary_instances.py::test_authenticated_private_promotion_cannot_change_public_observations`, `::test_explicit_public_history_does_not_override_an_unsafe_companion`, `::test_arming_requires_existing_history_assignment`, `::test_colliding_extensions_keep_their_own_cached_meaning`.
- 7.4 `tests/test_private_vocabulary_instances.py::test_authenticated_private_promotion_cannot_change_public_observations`, `::test_private_history_preserves_reason_without_touching_shared_logs`, `::test_prospective_selector_never_overrides_canonical_destination`, `::test_legacy_armed_registry_requires_explicit_assignment`, `::test_arming_requires_existing_history_assignment`.
- 7.6 `tests/test_semantic_units.py::test_unavailable_definitions_serve_only_units_no_custom_heading_encloses`, `tests/test_private_vocabulary_instances.py::test_a_live_revocation_withholds_warm_private_units_but_keeps_the_page_readable`.
- 7.7 `tests/test_private_vocabulary_instances.py::test_vector_candidates_take_their_selected_meaning_before_the_result_limit`, `tests/test_semantic_unit_reconcile.py::test_an_old_generation_reads_incomplete_until_reconcile_restores_coverage_and_lifecycle`, `tests/test_semantic_unit_embeddings.py::test_incremental_index_repairs_unit_rows_even_when_page_chunks_are_current`.

Open: 7.2 has no test that moves or reclassifies a page across instances, and 7.3 has no test that scopes usage counts to an instance.

- [x] 7.1 Reuse the merged registry foundation for core-plus-one-extension instances; verify typed references and canonical Scope binding for overlays and history.
- [ ] 7.2 Add registry and prospective page selection before lookup; recheck destinations and before/after bindings while preserving instance identity across consumers.
- [ ] 7.3 Scope collisions, aliases, folders, usage, cache identity, and hashes to admitted instances; prove private edits cannot affect public outcomes.
- [x] 7.4 Preserve promotion, restore, and protected history without private shared-log entries; prove direct-read protection, ambiguous bindings, and legacy assignment.
- [x] 7.6 Serve core interpretation under unavailable definitions only when every ancestor heading is core-recognized; prove raw reads stay useful, other units report unavailable, and coverage is incomplete, not empty.
- [x] 7.7 Store per-parent structural coverage in the existing embedding metadata table with occurrence vectors; prove missing coverage reads as incomplete and hidden or suppressed candidates spend no result slot.
- [ ] 7.5 Document domains and run paired acceptance, independent review, and ordinary release; record evidence before beginning managed-vault import.
- [ ] 7.8 Follow-up: FTS5 bm25() whole-table statistics include policy-withheld and tombstoned pages, so a restricted caller's scores depend on hidden pages; this is old debt on main too.

## 8. Delivery C: offline managed-vault import and recovery

- [ ] 8.1 Specify the bounded offline interface and recovery sequence using current mutation/restore owners; validate this change before implementing importer details.
- [ ] 8.2 Inventory source and destination completely with bounded private archive intake; prove every canonical object and unsupported entry has an explicit disposition.
- [ ] 8.3 Reconcile identities, paths, exact duplicates, and dependent conflicts; prove durable duplicate provenance and complete reference integrity before publication.
- [ ] 8.4 Preserve Sources, Evidence, Records, media, sidecars, units, history, relations, citations, and review provenance; prove source authority and indexes never become destination authority.
- [ ] 8.5 Import under stopped maintenance with verified copies, fingerprints, and a complete preimage; prove exact retries and crash recovery without overwriting later work.
- [ ] 8.6 Preserve plaintext-free append-only evidence and surviving copies through abort/rollback; prove receipts never become policy or knowledge and later edits remain accounted for.
- [ ] 8.7 Document offline operation and rehearse full import/rollback on disposable copies; verify allowed utility, hidden-corpus independence, and source immutability.
- [ ] 8.8 Obtain independent review and full completion verification, then merge/release; synchronize deltas and archive only when all required product work has shipped.

## 9. Separately authorized operations

- [ ] 9.1 Obtain authority for the named real import and connector cutover after rehearsal; verify fresh fingerprints and actual connector observations before changing routing.
- [ ] 9.2 Retain source and recovery copies after cutover; verify every imported bundle survives any proposed rollback or separately authorized source retirement.

Operational tasks require their own evidence. Product delivery and a checked implementation box do not supply operational authority.
