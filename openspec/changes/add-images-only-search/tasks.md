## 1. Restriction to file kinds on search_memory

- [x] 1.1 `search_memory` takes `file_types` and passes it to the leaf shortcut; the field description says when to set it. Evidence: `public_tools.SearchMemory.file_types`, passed in `_search` to the `file_types` shortcut of `op_ask_memory`.
- [x] 1.2 Regenerate the MCP tool schema baseline and record the new discovery digest as pending in the personal plugin contract. Evidence: `scripts/dump-tool-schemas.py` output in `tests/fixtures/mcp_tool_schemas.json` and `src/exomem/tool_surface_contract.json`; `deploy/chatgpt/personal-plugin-contract.json` pending digest; `tests/test_mcp_schema_fidelity.py` passes.
- [x] 1.3 Test: an agent's `search_memory` call with `file_types` set to `["image"]` returns only image sidecars through MCP. Evidence: `test_an_agent_restricts_a_search_to_images` (fails without the change). A host that still sends `file_types` inside `options` keeps working (`test_a_host_on_a_cached_tool_surface_can_still_restrict_a_search_through_options`).

## 2. CLIP lane inside a restriction to pictures

- [x] 2.1 Detect a restriction to pictures from the caller's compiled filter plan with the closed `media_types.VISUAL_KINDS`; run the CLIP lane unbanded and report `media_restricted` in the trace. Evidence: `structured_filters.requires_one_of`, called once per search in `find`; `find_candidates` takes `media_restricted`; `media_processing`, `media_worker` and `backfill` read the same `VISUAL_KINDS`; `test_a_filter_that_names_no_picture_kind_keeps_the_band`.
- [x] 2.2 `clip_media_restricted_weight` replaces the CLIP intent weight in fusion inside the restriction; set its default from the dev sweep. Evidence: default 5.0, the most dev targets (32 of 39) across 21 photos and 18 screenshots against weights 1, 2, 3, 10 and CLIP order alone (today: 29).
- [x] 2.3 Tests: a restricted search ranks photos the band would withhold; a restriction that admits notes keeps the band. Evidence: `test_a_search_restricted_to_images_ranks_the_photos_the_band_would_withhold` (fails without the change), `test_a_restriction_that_still_admits_notes_keeps_the_band`.
- [x] 2.4 Measure the cost at the load target (20,000 eligible pages, 100,000 CLIP rows). Evidence: the restriction check reads the filter plan once per search; the skipped band saves 2.7 ms; inside the restriction the CLIP lane skips the per-row sidecar check for indexed rows, which a review measured at about 60 ms for 20,000 rows.

## 3. Proof

- [x] 3.1 Dev replay on spent photo and text-bearing image sets chooses the weight under the rule written before the replay. Evidence: replay fidelity 39 of 39 against live top 8; rule "most total dev hits, tie to the lower weight".
- [x] 3.2 A fresh blind held-out set, keys written by an independent lane, measures the switch end to end, with real Sonnet callers choosing each call. Evidence: the two arms tied on 16 picture cases × 2 repeats (28 of 32 each; difference 0, interval [0, 0]), so the pre-written rule did not show a gain. The set sat near ceiling: only 6 unspent textless photo scenes exist. Both guards held: no text-bearing image lost net, and no caller restricted a text question. The owner chose on 24 Nov 2026 to ship the option as opt-in, with the run-both guidance for unclear cases.
- [ ] 3.3 After release, a live read-only run of the held-out picture cases with `file_types` matches the replay.
