# Tasks: add-referent-resolution

## 1. Pure predicate and registry

- [x] 1.1 test_cue_fires_on_counted_plural_person_noun: counted person cues expose type and count.
- [x] 1.2 test_cue_count_outside_window_is_ignored: distant counts do not bind.
- [x] 1.3 test_cue_silent_without_entity_noun: non-entity phrasing is silent.
- [x] 1.4 test_cue_organization_from_registry_aliases: registry aliases drive non-person cues.
- [x] 1.5 test_exact_name_resolves_alone: exact normalized title/alias is sufficient.
- [x] 1.6 test_fuzzy_name_needs_second_kind: fuzzy identity needs corroboration.
- [x] 1.7 test_graph_edge_from_non_entity_anchor_is_graph_evidence: released note anchors corroborate entities.
- [x] 1.8 test_links_to_edge_is_tier_one_graph_evidence: plain links are recorded at tier one.
- [x] 1.9 test_superseded_anchor_does_not_corroborate: stale anchors supply no graph evidence.
- [x] 1.10 test_anchor_beyond_cap_does_not_corroborate: graph work is prefix-bounded.
- [x] 1.11 test_attribute_overlap_matches_stem_or_prefix: attributes use the documented matcher.
- [x] 1.12 test_retrieval_presence_alone_is_candidate_not_resolved: one evidence kind abstains.
- [x] 1.13 test_type_mismatch_without_exact_name_is_dropped: cue type constrains non-exact candidates.
- [x] 1.14 test_inactive_entity_never_resolves_and_is_counted_in_reasons: lifecycle is enforced.
- [x] 1.15 test_partial_reports_unresolved_count: under-count reports the remainder.
- [x] 1.16 test_over_count_is_ambiguous_and_lists_all: over-count asks for disambiguation.
- [x] 1.17 test_no_count_zero_resolved_is_unresolved: empty uncounted resolution abstains.
- [x] 1.18 test_block_contains_no_floats: the block is categorical/integer only.
- [x] 1.19 test_resolution_is_permutation_invariant: input ordering cannot alter output.
- [x] 1.20 test_registry_reads_active_entities_with_aliases_and_attributes: authored identity fields load.
- [x] 1.21 test_registry_records_inactive_status_and_skips_non_entity_pages: lifecycle is retained and page type enforced.
- [x] 1.22 test_registry_skips_index_and_records_paths: navigation pages are excluded.
- [x] 1.23 test_registry_cache_hits_on_same_freshness_key_and_rebuilds_on_new_key: checkpoint cache invalidates exactly.
- [x] 1.24 test_cue_prefers_typed_noun_over_leading_interrogative: typed nouns outrank interrogative fallback cues.
- [x] 1.25 test_cue_count_survives_interrogative_prefix: a leading interrogative cannot discard the typed noun's count.
- [x] 1.26 test_partial_name_token_is_fuzzy_name_evidence: an exact token within a longer identity remains partial-name evidence.
- [x] 1.27 test_candidates_are_capped_deterministically_with_omitted_count: serialized matches are path-stable and bounded.

## 2. Runtime, envelope, and governance

- [x] 2.1 test_referents_block_emitted_only_when_cue_fires: the envelope is additive and cue-gated.
- [x] 2.2 test_referents_rides_compact_and_full_detail_identically: envelope detail does not alter resolution.
- [x] 2.3 test_referents_coexists_with_pack_timings_and_records_stage: pack and timings compose.
- [x] 2.4 test_referents_identical_on_hot_cache_hit: resolver output is recomputed identically post-cache.
- [x] 2.5 test_hits_are_byte_identical_with_resolver_on_and_off: hit ordering and shape stay fixed.
- [x] 2.6 test_graph_off_omits_graph_evidence: existing graph flag is the ablation arm.
- [x] 2.7 test_keyword_mode_never_runs_resolver: keyword recall pays no stage cost.
- [x] 2.8 test_resolver_exception_soft_fails_to_unchanged_response: runtime errors omit the block.
- [x] 2.9 test_kill_switch_env_disables_resolver: the environment switch disables the stage.
- [x] 2.10 test_ask_memory_and_find_share_the_referents_leaf: every product surface shares the leaf.
- [x] 2.11 test_product_case_two_counted_friends_one_captured: one represented identity yields partial plus one unresolved.
- [x] 2.12 test_referents_never_name_withheld_entity_pages: withheld entities cannot leak.
- [x] 2.13 test_referents_drop_evidence_naming_withheld_anchor_seeds: withheld anchors cannot leak through evidence.
- [x] 2.14 test_referents_block_omitted_for_blocked_audience: blocked policy is silent.
- [x] 2.15 test_referents_drop_tombstoned_entities_and_evidence_even_when_policy_is_empty: lifecycle deletions fail closed before policy routing.
- [x] 2.16 test_referents_release_decisions_are_receipted: every referent-path decision reaches the disclosure receipt.
- [x] 2.17 test_referents_block_omitted_when_guard_withholds_every_match: guarded emptiness cannot reveal a hidden match.
- [x] 2.18 test_referents_honour_release_strip_decisions: bridge stripping removes detail without dropping the released identity.
- [x] 2.19 test_resolver_exception_is_logged_and_soft_fails: additive resolver failures warn once and preserve hits.
- [x] 2.20 test_ask_cli_lists_hits_and_summarises_referents_for_cue_queries: human CLI output keeps hit listings and adds a referent summary.

## 3. Benchmark, performance, and guidance

- [x] 3.1 test_fixture_render_is_deterministic_for_seed: seeded corpus bytes are reproducible.
- [x] 3.2 test_fixture_passes_public_artifact_privacy_scan: committed fixture content is synthetic/public-safe.
- [x] 3.3 test_every_case_meets_expected_outcome_with_graph_on: cases A, A2, B, C, D, D2, E, F, G, H, I, and J meet their graph-on contract.
- [x] 3.4 test_graph_only_cases_fail_without_graph_and_pass_with_graph: graph value is measured by ablation.
- [x] 3.5 test_negative_control_and_ambiguity_cases_abstain: unsafe cases never false-resolve.
- [x] 3.6 test_metric_floors_hold: aggregate quality floors are enforced.
- [x] 3.7 test_report_is_aggregate_only_and_reproducible: public reports contain aggregates, not paths/cases.
- [x] 3.8 test_referent_stage_stays_bounded_at_scale: warm 2k stage remains below 1000 ms and skips non-cues.
- [x] 3.9 test_referent_stage_does_not_scale_linearly: 2k-to-8k stage ratio remains bounded.
- [x] 3.10 test_people_pages_document_aliases_and_about_entity: scaffold documents identity/edge guidance.
- [x] 3.11 test_search_guidance_teaches_referents_contract: bootstrap teaches abstention behavior.

## 4. Acceptance

- [x] 4.1 Run the focused acceptance shard and governance/scaffold/schema invariants.
- [x] 4.2 Run retrieval latency and golden quality gates.
- [ ] 4.3 Run the full lean suite.
- [x] 4.4 Run write-latency, capability, benchmark, lint, types, and strict OpenSpec gates.
