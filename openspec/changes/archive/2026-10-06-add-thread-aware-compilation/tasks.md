# Tasks: add-thread-aware-compilation

The orchestrator ruled on the design on 2026-09-29; the rulings are recorded in `design.md`. Every behaviour task is red-first: write the test, show it failing, then implement. Run tests scoped to the touched modules with `CUDA_VISIBLE_DEVICES= XDG_STATE_HOME=$(mktemp -d) uv run pytest -q -p no:cacheprovider <files>`.

## 0. Design ruling

- [x] 0.1 The orchestrator ruled on `focus` as current-turn evidence with origin labels, assistant entries in `recent`, the call-ledger hash, the latency scope, S1+S4 as one surface change, the branch name, and the attachment cues. The artifacts are amended to match, and `openspec validate --all --strict` passes.
- [x] 0.2 The orchestrator rules on retiring `claude/keyless-thread-continuity` unmerged (design D9). Until then, task 3.0 stands.
  Evidence: orchestrator ruled: the keyless branch is retired; 3.0 is audit-only.

## 1. S0: pre-registration and baseline (no product change)

- [x] 1.1 Red: `tests/test_context_activation_conversation_fixtures.py`. It proves the group holds everything below, and that editing any gold list changes the digest:
  - twelve rich turns and twelve multi-turn conversations;
  - one twin per case;
  - at least three drowning cases and three topic-switch cases;
  - one withheld-versus-absent pair;
  - two attachment cases with cue-only `focus`, plus a twin;
  - gold, poison, must-include, must-exclude, expected status, expected `carried_by` and expected `origin`;
  - invented names only;
  - no fixture turn verbatim in any corpus page.
  - Evidence: `tests/test_context_activation_conversation_fixtures.py` was written first and failed at collection (`ModuleNotFoundError: epistemic.corpora.context_activation_conversation`), then passed with 31 tests once the fixtures existed (commit d30a97e6).

- [x] 1.2 Author the fixtures and pin their digest:
  - add `FixtureCase.conversation` (optional) and the group ids;
  - make the corpus additions through supported writers: entity pages, hubs, one Records collection, and one governed page withheld from a restricted audience;
  - add a new `FIXTURE_SET_ID` and digest, pinned in the test.

  Commit this before any scored run.
  - Evidence: `benchmarks/epistemic/corpora/context_activation_conversation.py`, `FIXTURE_SET_ID = context-activation-conversation-v1`, digest `374056f5fc22d9a75249b83162d141a5b293b2fc5fc68071ebcad88a2fd5170f` pinned in the test; 55 cases (12 rich turns, 13 conversations, 2 attachments, the withheld pair, 26 twins). The English digest `a49d85f4…` is unchanged and asserted. The sibling `ConversationCase` carries `conversation`, so the digest-pinned English `FixtureCase` is untouched. Entity, hub, resource and Records pages use the English module's writers; the withholding scope and ceiling-0 rule are written as the governance tests write them, because `govern_memory` commit refuses on a cold fixture vault (`prepared composite does not match`). Committed before any scored run (d30a97e6).

- [x] 1.3 Add scorer arms a to d in `benchmarks/membench/utility/context_activation.py`:
  - drowning counts as a case failure;
  - the withheld pair is scored for byte identity;
  - arm (a) gives the mechanism-removal verdict;
  - `origin` is checked against the expectation.

  Add the new test module to `tests/harness_modules.txt` if it imports `benchmarks/`.
  - Evidence: `benchmarks/membench/utility/context_activation_conversation.py` scores arms a to d per case and per anchor kind with duals and no aggregate; drowning fails a case outright; the withheld pair is scored for byte identity; the arm (a) mechanism-removal verdict and the `origin` check are in; the new test modules are in `tests/harness_modules.txt`. Sibling module, not a change to `context_activation.py`, mirroring the multilingual scorer.

- [x] 1.4 Run arm (a) on current `main` and record the baseline manifest with the fixture digest. Confirm that the incident classes fail and that the attachment cases abstain. Arms b to d are expected to be refused as unknown arguments.
  - Evidence: Arm (a) on `main` fails 9 of 13 multi-turn cases (mechanism-removal arm red); promotion V13-V14, tie-break V15-V16, anaphoric carry V17-V18 and topic switch V19-V21 all fail; both attachment cases abstain; all 26 twins, the three drowning cases and the refs-only case pass; 82 conversation-bearing arm b to d requests are recorded `refused: unknown argument`. Manifest: `baseline-arm-a.json`.

## 2. S1+S4: argument, evidence and surface, shipped as one surface change

- [x] 2.1 Red: bounding tests covering:
  - oversized, malformed and empty input;
  - the `generation.conversation` values;
  - a packet without `conversation`, byte-identical to the pre-change packet apart from the new field, with every anchor labelled `origin = "turn"`;
  - no `working_set.conversation` span on a request without `conversation`;
  - door parity across MCP, CLI and REST.
  Evidence: tests/test_working_set_conversation_bounds.py (14 tests; red on the missing argument, green).
- [x] 2.2 Red: privacy tests covering:
  - a sentinel phrase absent from every state file, and its sha256 and the serialised argument's sha256 absent from every file, the call ledger included;
  - the cache bypass;
  - no hot-profile event from refs;
  - the token never encoding conversation-only anchors;
  - activation-log fields limited to presence, counts and the generation value.
  Evidence: tests/test_working_set_conversation_privacy.py (state-file and hash scan, log fields, cache bypass, heat, token).
- [x] 2.3 Red: a withheld-ref twin is byte-identical to the absent one, `generation` included.
  Evidence: withheld-ref twin tests in the privacy module, generation included.
- [x] 2.4 Red: the `focus` segment.
  - It uses worded kinds only, with no second recall or embedding.
  - Segments never pair.
  - A referential turn stays referential when `focus` is present.
  - Anchors carry `origin` values `turn`, `focus` and `turn_and_focus`.
  - A `focus` origin is never `agent_choice`.
  - `focus` does not settle an ambiguity the turn segment resolved.
  Evidence: tests/test_working_set_conversation.py (focus segment, origins, referential, ambiguity).
- [x] 2.5 Red: attachment cues.
  - A content-free turn plus cue-only `focus` resolves with `origin = "focus"`.
  - The same turn without `focus` abstains.
  - A cue that names nothing leaves the packet unchanged.
  - Activation opens no media module, asserted by a guard on the media and vision imports during the call.
  Evidence: attachment cue tests with a media-import guard, same module.
- [x] 2.6 Red: the `conversation` qualifier.
  - It promotes a partial second domain.
  - It resolves nothing alone.
  - It never matches a name split across entries.
  - Only the newest three user and two assistant entries are read.
  - It counts once together with `continuity`.
  Evidence: qualifier tests, same module.
- [x] 2.7 Red: the tie-break. Exactly one competitor carrying `conversation` settles the turn; two competitors keep the ambiguity.
  Evidence: tie-break tests, same module.
- [x] 2.8 Red: the drowning guard.
  - A long conversation about one subject does not resolve that subject for a turn about another.
  - Promoted material stays within a third of the budget and is served after the turn's own anchors.
  Evidence: drowning tests, same module.
- [x] 2.9 Red: the surface.
  - The server instructions contain the pinned sentence, including the attachment clause, keep "verbatim", and pass the existing length test. If the length test fails, apply design D5's fallback and update the spec in the same PR.
  - The tool description states the bounds, the attachment cues, the origin meaning, and that activation reads no attachment.
  - The scaffold line is updated, and `tests/test_scaffold_no_leak.py` passes.
  Evidence: tests/test_working_set_conversation_surface.py. Per the orchestrator's ruling on #1455 the guidance is in the tool description and the instructions carry a short pointer (892 of 900 characters, bound unchanged).
- [x] 2.10 Implement:
  - in `working_set.py`, `working_set_resolve.py` and `working_set_runtime.py`;
  - the `commands.py` leaf and CLI flags;
  - `server.py` (`SERVER_INSTRUCTIONS`);
  - the scaffold `SKILL.md`;
  - the timing span `working_set.conversation`, with the reserve-skip fallback.
  Evidence: working_set_conversation.py, working_set*.py, commands.py, __main__.py, server.py, scaffold SKILL.md, span working_set.conversation with the reserve-skip.
- [x] 2.11 Regenerate the derived surfaces:
  - `scripts/dump-tool-schemas.py`, which writes the schema fixture and the tool-surface contract;
  - the plugin trees and the hosted render (`scripts/hosted-plugin.py`);
  - the v5 candidate;
  - `docs/capabilities.md`;
  - the README tool table.
  Evidence: dump-tool-schemas, generate-capabilities, package-skills, refresh-skill-contract run; ChatGPT contract pending digest cf4589af... with refresh_required. Hosted candidates and v5 render untouched (activate_context is not on the hosted surface; hosted plugin tests pass).

  Set `pending_tool_surface_sha256` and `refresh_required` in `deploy/chatgpt/personal-plugin-contract.json` per `docs/remote-quickstart.md`.
- [x] 2.12 Run the gates:
  - scoped pytest;
  - `uvx ruff check --select F src tests`;
  - the privacy gate;
  - `generate-capabilities.py --check`.
  Evidence: re-run on current `main` plus the 2026-10-06 compiler follow-up: the working-set gate (`tests/test_working_set_*.py` and `tests/test_activat*.py` at b0137cc69: 2,547 passed, 21 skipped), which holds the working-set conversation suites and `tests/test_working_set_round8.py`; `ruff check . --select F` clean; the privacy gate clean; `generate-capabilities.py --check` current. The refresh is handed off in `deploy/chatgpt/personal-plugin-contract.json`: pending digest `dbd80184…` with `refresh_required: true` and `rollout_state: awaiting-post-deploy-refresh`, the registered and verified digests unchanged. It stays release-blocking for that connector until the owner verifies it, as for every surface change since 0.45.0.

  Then hand the owner the one refresh of the ChatGPT Personal Plugin and the claude.ai connector. Promote the digest only after the owner verifies it.

## 3. S2: the conversation carry and the precedence ladder

- [x] 3.0 Re-run the D9 audit of `claude/keyless-thread-continuity` against current `main`:
  - compare the branch's test functions with main's;
  - diff `working_set_resolve.py`;
  - find the `:stranger` salt;
  - trial-merge in a scratch worktree.
  Evidence: audit on current main: no test function only on the branch, working_set_resolve.py identical, ':stranger' salt present on main, trial merge conflicts in the same six files. Nothing residual, nothing merged.

  If a behaviour or test exists only on the branch, merge the branch into the build branch first, resolving every conflict toward `main`'s later contract (authenticated threads, the `withheld` exception). Include that merge in this PR's review. If nothing is residual and the orchestrator has ruled on 0.2, follow that ruling.
- [x] 3.1 Red: anaphoric turns and the carry.
  - The anaphor set covers pronouns, possessives, demonstratives, the shipped follow-up markers, an ordinal plus "one" or "option", and "former" or "latter".
  - A long anaphoric turn is carried from the newest user entry that resolves an anchor, with `origin = "conversation"`.
  - The newest subject beats an older one.
  - Two anchors in that entry make the turn `ambiguous`.
  - A refs-only conversation never carries.
  - A turn that names its own subject is not carried.
  - The carried anchor is `partial` and absent from the token.
  Evidence: tests/test_working_set_conversation_carry.py (anaphor set, carry, newest-wins, ambiguity, refs-only, assistant never walked).
- [x] 3.2 Red: the precedence ladder.
  - Referential recency, the continuity-thread resume and the shipped follow-up carry each still win, with `carried_by` unchanged.
  - The conversation carry wins over the retrieval carry.
  - A conversation carry that abstains `ambiguous` stops the retrieval carry.
  - Without `conversation`, every existing carry behaves byte-identically.
  Evidence: ladder tests in the same module (recency, follow-up, conversation before retrieval, ambiguous stops the ladder).
- [x] 3.3 Implement the carry walk and the ladder position. Rerun, unchanged, the S1 tests and the existing continuity, keyless, follow-up, carry and hot-projection suites:
  - `test_working_set_continuity.py`
  - `test_working_set_keyless_continuity.py`
  - `test_working_set_carry.py`
  - `test_working_set_hot_projection.py`
  Evidence: carry and ladder implemented; test_working_set_continuity, keyless, carry, hot projection suites green (596 passed across the recheck).

## 4. S3: hooks

- [x] 4.1 Record transcript fixtures with invented content only. For Claude Code JSONL, cover:
  - user and assistant text;
  - a tool call and its result;
  - a thinking block;
  - an injected Exomem block;
  - the current prompt already appended.
  Evidence: tests/hook_transcripts/ (invented Claude Code and Codex-shaped fixtures). Codex UserPromptSubmit transcript_path delivery could not be verified offline: Codex parsing is exercised on a rollout-shaped fixture only, and where the path is absent the hook sends no conversation.

  Record a Codex rollout fixture once its `UserPromptSubmit` payload is verified to carry `transcript_path`, or record that it does not.
- [x] 4.2 Red: hook tests covering:
  - which entries are extracted, what is excluded, and the order;
  - refs taken from call arguments only;
  - no `focus` sent;
  - the 50 ms budget, failing silent;
  - one retry without the field on an older service;
  - no conversation bytes in hook state;
  - origin labels rendered for `conversation`;
  - an all-`turn` packet rendered byte-identically to today.
  Evidence: tests/test_retrieve_nudge_conversation.py (22 tests).
- [x] 4.3 Implement in `src/exomem/_hooks/exomem_retrieve_nudge.py`. Regenerate the plugin mirror with `exomem package-skills --plugin-root plugins/claude-code` and pass `tests/test_plugin_sync.py`.
  Evidence: hook implemented; plugin mirror byte-identical, tests/test_plugin_sync.py green.

## 5. S5: acceptance and latency

- [x] 5.1 Run the `conversation` group on arms a to d against the pinned digest.
  - Publish per-case, per-anchor-kind metrics with their duals, and no aggregate.
  - Arm (a) must come out red, and arms b to d must meet the floors.
  Evidence: acceptance-run.json and tests/test_context_activation_conversation_acceptance.py on the corrected digest `1c81d3e9…`: every scored row on arms a to d meets its pre-registered expectation except arm (a)'s intended failures; PENDING_RULING is empty. Post-hoc fixture correction recorded in design.md.
- [x] 5.2 Pin these in the CI latency gate, with the measured numbers recorded in the manifest (maximum-size conversation, synthetic reference corpus):
  - `working_set.conversation` p95 ≤ 60 ms;
  - warm activation p95 ≤ 1,000 ms.
  Evidence: tests/test_latency_gate.py::test_a_maximum_size_conversation_stays_within_the_stage_and_warm_p95_budgets; measured in acceptance-run.json (stage p95 32.4 ms, warm p95 169.5 ms).

  Record separately that live-cell latency is owned by its own lane and was not regressed: a request without `conversation` records no conversation stage.
- [x] 5.3 In the delivery that completes 5.1 and 5.2, sync the deltas into the canonical specs and archive with `openspec archive`. Run `openspec validate --all --strict` before and after.
  Evidence: synced and archived with `openspec archive add-thread-aware-compilation` in the 2026-10-06 compiler follow-up, after 6.1 to 6.4 and 6.6 were evidenced. The `call-ledger` MODIFIED block matched the canonical requirement plus this change's sentence and scenario, so no later requirement was dropped. `openspec validate --all --strict` passed before and after.

## 6. Round-eight correction and independent acceptance

- [x] 6.1 Red-first regression evidence for locally introduced task/time/numeric/neutral referents, quoted expressions, local value choices and valid longer closing fences; retain the permitted content-free and historical-reference controls without widening the frozen task vocabulary.
  Evidence: commit f80614296, delivered in #1477 (7fd5429aa). `tests/test_working_set_round8.py`: `test_assigned_nominals_and_delimited_values_veto_history`, `test_container_code_keeps_its_whole_body_opaque`, `test_container_fences_end_at_the_close_or_the_container_boundary`, `test_local_material_vetoes_even_an_empty_title_licensed_residue`, `test_internal_quote_and_code_pointers_never_point` and `test_additional_introductions_and_nominal_alternatives_stay_local`; the controls `test_historical_controls_and_apostrophes_keep_their_licence` and `test_quantified_information_requests_keep_the_historical_relation`. Red: that commit's module fails 214 of 270 cases on its parent f80614296~1 (151 assertions, 50 on the missing `local_material`); green on current `main`. `TASK_VOCABULARY` is unchanged from that parent to `main`.
- [x] 6.2 Implement the deterministic local-material veto before lexical erasure/title licensing and the distinct inline/fenced code treatment; preserve shipped carry precedence and never search an older subject after failed eligibility.
  Evidence: commit f80614296 in #1477. `working_set_anaphora.surface_analysis` returns the turn-local veto (`_ConstructionScanner.local_material`) with the pointer tokens, before lexical erasure; inline runs and fenced blocks are read apart (`_opaque_spans`); `working_set_conversation.may_carry` refuses on `local_material` before the title licence. Tests: `test_local_material_never_attaches_a_historical_subject`, `test_a_local_veto_does_not_walk_any_historical_subject`, `test_current_turn_and_focus_resolution_precede_the_local_veto`, `test_inline_runs_require_an_exact_match` and `test_opaque_code_cannot_gain_a_title_licence`.
- [x] 6.3 Red-first inclusive footprint checks using real indexed long titles, no-material fallback, ambiguity menus, authored metadata, recent context and post-egress removal; compare ordinary packets unchanged.
  Evidence: commit f80614296 in #1477. `tests/test_working_set_round8.py`: `test_titles_spend_the_inclusive_boundary`, `test_real_indexed_long_titles_are_bounded_including_no_material_fallback`, `test_real_indexed_ambiguity_keeps_every_identity_with_budgeted_titles`, `test_unaffordable_authored_header_metadata_is_empty_without_losing_identity`, `test_complete_authored_metadata_payloads_must_fit`, `test_state_and_overflow_pointer_occurrences_are_charged` (recent context included), `test_egress_recounts_retained_subject_prose_without_refill` and `test_ordinary_packet_accounting_keeps_its_shipped_fields`. Red on the parent and green on `main`, as in 6.1.
- [x] 6.4 Implement one conversation-only prose ledger for all inferred packet paths and post-egress reconciliation; preserve identities and ordinary accounting.
  Evidence: commit f80614296 in #1477. `working_set_conversation.InferredPacket`, `prose_chars`, `budget_headers` and `subject_chars` are the one ledger that `build_packet` and `abstained_packet` use for every inferred packet, and `guard_working_set` recounts an `InferredPacket` after it removes anything. Tests: `test_egress_recounts_retained_subject_prose_without_refill`, `test_command_leaf_preserves_inferred_marker_through_egress`, `test_selected_custom_role_identities_are_exempt_at_each_occurrence` and `test_ordinary_packet_accounting_keeps_its_shipped_fields`.
- [x] 6.5 Obtain author-independent recheck with a genuinely new frozen private set at both public compiler and command leaf: topic-switch false carries ≤5%, content-free carry/recall disclosed, every inclusive subject footprint within its third. Disclosed sets are regression evidence, not a new gate.
  Evidence: 2026-10-01 independent R12 review approved the frozen construction-scanner replacement. Each guarded public path had 2/48 false carries (4.17%) on the fixed unseen material corpus, with no exclusions or denominator changes; content-free carry was12/12 and historical subject recall10/12. All13 disclosed R11 failures were repaired.194 public calls each observed one active egress guard; inclusive subject prose64<=400 and total packet144/1200. Focused existing suite360 passed;8192-delimiter calls took0.0307/0.0383 CPU seconds. Two P3 precision residuals remain counted. Integration and delivery are still required by6.6.
- [x] 6.6 Verify corrected scoped suites, privacy, surfaces and strict OpenSpec; at delivery run completion-boundary checks, refresh main after the release hold, and close/archive only with actual integration evidence.
  Evidence: integrated in #1477 (merge 7fd5429aa on 2026-10-01, 28 checks green, released in v0.102.0). Re-verified on current `main` plus the 2026-10-06 compiler follow-up: the working-set gate (`tests/test_working_set_*.py` and `tests/test_activat*.py` at b0137cc69: 2,547 passed, 21 skipped); the privacy gate clean; the derived surfaces regenerated to a fixed point (`refresh-skill-contract.py`, `package-skills`, `dump-tool-schemas.py`, the v5 hosted renders and `hosted-plugin.py check`, `cloud-plugin.py build` and `check`, `generate-capabilities.py --check`); `openspec validate --all --strict` passed.
