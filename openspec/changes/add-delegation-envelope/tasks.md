# Tasks — add the delegation envelope

Every test lands red first (verbatim failing output recorded before the
implementation, then green). Measured budgets are written into this file when
the task is ticked, not estimated.

## 1. Envelope core (design D1–D3)

- [x] 1.1 `src/exomem/envelope.py` (import-cheap, torch-free): the closed class
  enum; per-class ranges incl. the `confirm-shortcut` definition; ceilings;
  `derive_envelope(level)` pinned by test against the design table; write-time
  refusals for unknown class, out-of-range disposition, and `disclosure`;
  read-time report-and-ignore for unknown stored ids (red-first: a config with
  a future class id still serves and names it).

  Evidence (red, before `src/exomem/envelope.py` existed) —
  `pytest tests/test_delegation_envelope.py -q`:

  ```
  ImportError while importing test module '/…/tests/test_delegation_envelope.py'.
  tests/test_delegation_envelope.py:25: in <module>
      from exomem import envelope, mode
  E   ImportError: cannot import name 'envelope' from 'exomem'
  ERROR tests/test_delegation_envelope.py
  !!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
  1 error in 0.11s
  ```

  Evidence (green) — same command: `22 passed in 0.12s`.
- [x] 1.2 Shared-config storage: `envelope` object in `mode.config_path()`;
  overrides persist across restarts and prominence changes; reset per class;
  absent key = derivation (rollback pin). Red-first over a temp config root
  (no real state root — fixture discipline per `tests/conftest.py`).

  Evidence: the storage tests live in the same module and were red under the
  same collection error recorded for 1.1 (`test_an_override_outlives_a_prominence_change_and_a_restart`,
  `test_the_override_lives_in_the_shared_config_beside_mode_and_prominence`,
  `test_reset_restores_pure_derivation_for_the_named_class_only`,
  `test_deleting_the_envelope_object_restores_todays_shipped_behaviour`).
  Green in the `22 passed` run above. Every one drives `EXOMEM_CONFIG_PATH` at a
  `tmp_path` file, so no real state root is touched.
- [x] 1.3 Serve the envelope: bootstrap engagement block carries every class
  with ceiling, disposition or governance-owned marker, and
  fixed/derived/override provenance. Red-first pin on the payload.

  Evidence (red) — `pytest tests/test_delegation_envelope.py -q`, the five
  serving tests only:

  ```
  >       return commands.op_bootstrap(vault, profile=profile)["engagement"]["envelope"]
  E       KeyError: 'envelope'
  FAILED …::test_bootstrap_serves_every_class_with_ceiling_and_provenance[compact]
  FAILED …::test_bootstrap_serves_every_class_with_ceiling_and_provenance[full]
  FAILED …::test_bootstrap_serves_every_class_with_ceiling_and_provenance[diagnostics]
  FAILED …::test_the_served_envelope_moves_with_the_active_level
  FAILED …::test_a_stored_unknown_class_still_serves_bootstrap_and_names_the_id
  5 failed, 22 passed in 1.72s
  ```

  Evidence (green) — same command: `27 passed in 0.82s`.

  Measured cost of the served block alone (empty fixture vault, no overrides):
  compact 60,885 -> 61,531 bytes, **+646**. That is over the 61,400
  `COMPACT_BYTE_CEILING` by 131 bytes, and it stays over until task 4.2, which
  owns the byte budget and lands the protocol text and the single ceiling
  decision together. Recorded here so the intermediate state is not a surprise.

## 2. Ceiling enforcement (design D1, D6)

- [x] 2.1 Tiered confirm-required test: with the most permissive envelope and
  `maximal` prominence — (a) the served envelope still marks
  `restructure_execution` confirm-required, (b) `op_delete` still requires its
  explicit `confirm` parameter, (c) the adoption apply surface still defaults
  preview-first. Mechanism-removal proof: a scratch mutant that lets the
  envelope override the served confirm-required marker fails (a). No new
  server-side confirm parameters; the served contract names the
  supersession/entity-creation gap as future work.

  Evidence (red) — `tests/test_delegation_envelope_ceilings.py`, the served
  future-work clause, before `envelope.CONFIRM_REQUIRED` existed:

  ```
  >       clause = commands.op_bootstrap(root, profile="compact")["engagement"]["envelope"][
              "confirm_required"
          ].lower()
  E       KeyError: 'confirm_required'
  FAILED …::test_the_served_contract_states_the_server_side_gap_rather_than_implying_it_away
  1 failed, 15 passed in 0.73s
  ```

  Evidence (mechanism removal, scratch mutant — run, recorded, NOT committed).
  The mutant lets the envelope lift the marker: `derive_envelope` returns
  `silent` for `restructure_execution` at `maximal`, and `resolved()` stops
  treating that class as fixed. Tier (a) goes red:

  ```
  >       assert served["classes"]["restructure_execution"] == {
              "ceiling": "confirm-required",
              "disposition": "confirm",
              "provenance": "fixed",
          }
  E       AssertionError: assert {'ceiling': '...e': 'derived'} == {'ceiling': '...nce': 'fixed'}
  E         Differing items:
  E         {'disposition': 'silent'} != {'disposition': 'confirm'}
  E         {'provenance': 'derived'} != {'provenance': 'fixed'}
  FAILED …::test_maximal_prominence_with_every_override_still_marks_confirm_required
  1 failed in 0.64s
  ```

  Evidence (green, mutant reverted) —
  `pytest tests/test_delegation_envelope_ceilings.py tests/test_delegation_envelope.py -q`:
  `43 passed in 1.03s`. Tiers (b) and (c) pin gates that already exist and must
  STAY: `op_delete(..., confirm=False)` raises `UNCONFIRMED` and leaves the file
  in place; adoption `apply` refuses `INVALID_PHASE` before a plan exists and
  `PLAN_STALE` when the plan id is not echoed, with nothing copied into
  `Sources/`. No server-side confirmation parameter and no tool-schema change
  was added — `tests/test_mcp_schema_fidelity.py` (the packaged tool-surface
  digest) stays green, recorded under 5.2.
- [x] 2.2 Founder-gate refusal is the sole error for `restructure_execution`:
  any non-confirm disposition request refuses naming the gate; the generic
  range refusal never fires for this class. Red-first both halves.

  Evidence. The founder-gate branch shipped with 1.1's refusal ordering (the
  order is the mechanism: the class is checked before its value, so a standing
  delegation request can never read as a typo), so red was produced by REMOVING
  it rather than by not having written it yet — which is the same proof and is
  recorded verbatim. Mutant: delete the
  `if name == "restructure_execution": raise …STANDING_DELEGATION_REFUSED` branch
  and let the class fall through to the generic refusals.

  ```
  E         'ENVELOPE_CLASS_FIXED' is contained here:
  E           ENVELOPE_CLASS_FIXED: restructure_execution is fixed at 'confirm' and carries no range
  FAILED …::test_every_restructure_execution_request_refuses_by_naming_the_founder_gate[silent]
  FAILED …::…[advisory]  FAILED …::…[off]  FAILED …::…[always-allow]
  FAILED …::…[confirm]   FAILED …::…[]     FAILED …::…[from now on]
  FAILED …::test_the_generic_range_refusal_never_fires_for_restructure_execution[silent]
  FAILED …::…[always-allow]  FAILED …::…[]
  10 failed, 1 passed, 5 deselected in 0.55s
  ```

  That is exactly the failure this task exists to prevent: the generic
  fixed-class message shadowing the founder gate. Green with the branch
  restored, in the `43 passed` run above.

## 3. Adaptation (design D4)

- [x] 3.1 Quiet-offer derivation from durable manual-origin dismissal records
  (event count, monotonic, automatic origin excluded): third event arms exactly
  one offer; `quiet_offered_at` recorded; cleared only by explicit family reset
  to `normal`; a decline without reset never re-offers. Red-first through the
  review-state store, incl. a records-not-live-index pin (deleting the
  dismissed items does not disarm the offer) and a no-usage-signals structural
  pin (the derivation reads review-state records only).

  Evidence (red) — `pytest tests/test_delegation_envelope_adaptation.py -q`,
  before any of section 3 was implemented:

  ```
  FAILED …::test_the_third_manual_dismissal_arms_exactly_one_offer
  FAILED …::test_an_automatic_origin_decision_never_counts
  FAILED …::test_the_count_is_taken_from_the_records_not_the_live_index
  FAILED …::test_the_offer_is_recorded_durably_against_the_family
  FAILED …::test_a_decline_without_a_reset_never_re_offers
  FAILED …::test_quieting_the_family_keeps_the_offer_marker
  FAILED …::test_an_explicit_reset_clears_the_slate_and_one_new_offer_may_appear
  FAILED …::test_the_derivation_reads_review_state_records_and_nothing_else
  FAILED …::test_the_offer_changes_nothing_by_itself
  FAILED …::test_the_schema_version_moved_once
  FAILED …::test_a_previous_schema_store_is_migrated_on_load_and_rewritten_on_write
  FAILED …::test_a_dismissal_record_carries_the_family_that_produced_the_signal
  FAILED …::test_the_family_slate_holds_an_offer_while_the_disposition_is_normal
  13 failed, 3 passed in 3.29s
  ```

  Evidence (green) — same command: `16 passed in 3.88s`.

  Two design points the tests forced and that are worth reading before changing
  anything here. **Events are counted by `item_id`, not by record**: one triage
  decision fans out across every component signal a fused item holds, so
  counting records charged one family three events for one dismissal. **An offer
  needs a surfacing to ride on**: a family with nothing left to show has none,
  which is why the fixture keeps a sixth item permanently open — otherwise "no
  second offer" would mean "there was nothing to offer".
- [x] 3.2 The offer changes nothing by itself: disposition unchanged until an
  explicit decision lands. Red-first.

  Evidence (red) — `test_the_offer_changes_nothing_by_itself` in the 13-failure
  run above. Evidence (green) — in the `16 passed` run. With the offer standing,
  the family's disposition is still `normal`, the same items are still open, the
  family is still on the daily surface, and every envelope class disposition is
  byte-identical (`envelope.resolved()` compared before and after).
  `test_repeated_surfacing_without_triage_adapts_nothing` pins the other half:
  six surfacings with no triage arm nothing and leave no slate.
- [x] 3.3 Review-state schema migration: dismissal records gain family
  attribution and the family slate gains a durable `quiet_offered_at` slot
  that survives a `normal` disposition; one version bump, previous-schema
  files migrated on load and rewritten on next write, newer schema refused by
  an older runtime with a named error. Red-first on all three behaviours over
  fixture files at both versions.

  Evidence (red) — `test_the_schema_version_moved_once`,
  `test_a_previous_schema_store_is_migrated_on_load_and_rewritten_on_write`,
  `test_a_dismissal_record_carries_the_family_that_produced_the_signal` and
  `test_the_family_slate_holds_an_offer_while_the_disposition_is_normal` in the
  13-failure run above. Green in the `16 passed` run.

  One bump: `SCHEMA_VERSION` 2 -> 3, `_READABLE_SCHEMA_VERSIONS` `{1,2}` ->
  `{1,2,3}`. A v2 file migrates in memory on load (its records simply carry no
  `family`, and an unattributed record counts for nothing — the store cannot
  invent an attribution it never had) and is rewritten as v3 on the next write;
  a v4 file is refused with `REVIEW_STATE_INVALID: unsupported review state
  schema`, which is exactly how a v2 runtime now refuses a v3 file.

  Two existing pins restated the old version as a literal and were repaired to
  DERIVE it from `review_state.SCHEMA_VERSION`, per this repository's own rule
  that a pin reads its expected value from the canonical source:
  `tests/test_review_state_scaling.py::test_the_store_has_no_schema_retention_or_compaction_today`
  and `::test_a_previous_schema_store_keeps_its_decisions`. Both green
  (`18 passed in 5.00s`); neither assertion was weakened.

  Known boundary, stated rather than discovered later: family attribution is
  written where the caller can NAME the family — the review-item path
  (`apply_for_item`), which covers every registered attention family. Triage of
  a write-advisory ref (`near-duplicate`, `contradiction-band`, `overlap`) does
  not carry its kind through the ref, so those records stay unattributed and
  their families never arm an offer. Attributing them needs the kind on the
  advisory identity, which is a change to that surface rather than to this one.

  Regression scope run after the migration:
  `pytest tests/test_attention.py tests/test_review_state.py
  tests/test_review_dispositions.py tests/test_review_reason_and_origin.py
  tests/test_review_state_scaling.py tests/test_review_context.py
  tests/test_first_surfaced_ledger.py tests/test_corpus_aware.py
  tests/test_relation_queue.py tests/test_epistemic_review_queues.py -q`
  -> `234 passed, 4 skipped` (skips are `sentence_transformers`, absent by
  design in this environment), plus
  `tests/test_due_state_*.py tests/test_relation_queue_commands.py
  tests/test_adoption_proposals.py tests/test_epistemic_bootstrap_contract.py`
  -> `197 passed`.

## 4. The dispositions view and the agent contract (design D5; command-surface delta)

- [x] 4.1 `review_memory(mode="dispositions")` gains the structurally separate
  envelope block (class, ceiling, disposition/marker, provenance). If the
  recorded response contract or packaged digest moves, follow the documented
  two-phase rollout and record it here. Red-first pin on the two-block shape.

  Evidence (red) — `pytest tests/test_delegation_envelope_surfaces.py -q`:

  ```
  >       before = commands.op_review_memory(vault, mode="dispositions")["envelope"]["classes"]
  E       KeyError: 'envelope'
  FAILED …::test_the_dispositions_view_carries_two_structurally_separate_blocks
  FAILED …::test_the_view_says_which_off_is_which
  FAILED …::test_the_envelope_block_is_present_even_when_no_family_is_quiet
  FAILED …::test_a_family_decision_moves_no_envelope_class
  4 failed in 1.05s
  ```

  Evidence (green) — same command: `4 passed in 0.77s`.

  **No rollout was needed.** The change is additive to a response BODY; no tool
  input schema moved and the packaged tool-surface digest did not move:
  `pytest tests/test_mcp_schema_fidelity.py tests/test_tool_surface_contract.py
  tests/test_review_dispositions.py tests/test_command_surface_retry.py -q`
  -> `124 passed`. The two-phase response-contract rollout therefore does not
  apply to this landing.

  One behaviour changed in the family block and it is deliberate: a slate-only
  row (a family carrying `quiet_offered_at` while its disposition is still
  `normal`) is filtered OUT. This view answers "what have I quieted", and a row
  reading `normal` would be an answer to a different question.
- [x] 4.2 Bootstrap + scaffold/plugin SKILL.md copies + hookless
  custom-instructions block teach the decider protocol and how to discover the
  registered family vocabulary; ≤ 50 lines per carrier, counted by test;
  compact payload measured before/after and recorded here within the
  `COMPACT_BYTE_CEILING` discipline. Scaffold no-leak and skill-sync tests
  stay green.

  Evidence (red) — `pytest tests/test_delegation_envelope_surfaces.py -q`:

  ```
  >       assert "bootstrap" in section
  E       AssertionError: assert 'bootstrap' in ''
  FAILED …::test_every_carrier_teaches_the_envelope_within_its_line_budget
  FAILED …::test_every_carrier_states_the_decider_protocol
  FAILED …::test_every_carrier_names_the_founder_gate
  FAILED …::test_the_hookless_block_defers_to_the_served_envelope
  4 failed, 6 passed in 1.19s
  ```

  Evidence (green) — same command: `10 passed in 1.08s`.

  **Measured lines per carrier** (budget 50; the served payload has no lines of
  its own, so its measure is one line per scalar leaf — every distinct thing the
  client is told):

  | Carrier | Measured lines |
  |---|---|
  | compact bootstrap (`engagement.envelope`) | 25 |
  | scaffold `SKILL.md` (`## What Exomem does on its own`) | 46 |
  | plugin `SKILL.md` (same section, generated from the scaffold) | 46 |
  | hookless custom-instructions block (`docs/prominence.md`, same heading) | 26 |

  **Measured compact bytes.** Before 60,885 — exactly the figure the previous
  ceiling entry recorded, so nothing else moved in between. After **62,756**:
  **+1,871**, itemised as `classes` 610, `protocol` 622, `confirm_required` 323,
  `founder_gate` 204, `level` 20, plus 75 for the `family_disposition_reading`
  amendment that names the registered-family vocabulary and the envelope.

  `COMPACT_BYTE_CEILING` 61,400 -> **63,300**, with the full argument and the
  itemisation written into the constant's own docstring as that discipline
  requires. 544 bytes of headroom, just clear of `HEADROOM_WARNING_BYTES`.
  `MINIMUM_SAVING_RATIO` untouched; the saving moved 35.29% -> 34.60%. Two
  trims were taken first out of this change's own text: `confirm_required` no
  longer restates protocol step 3's "obtain the confirmation first" (-80 B), and
  `ignored` is emitted only when a stored value could not be used rather than as
  an always-present empty list (-15 B per session).

  One neighbouring pin moved and it is recorded rather than nudged:
  `tests/test_record_public_surface.py::test_compact_bootstrap_puts_record_route_before_semantic_authoring`
  asserted `"record"` appears within the first 8,192 bytes. That offset is a
  PROXY for "reachable early" — the real ordering property is the relative
  assertion beside it — and `engagement` precedes the action catalogue by
  design, so the proxy was re-cut to 10 KiB (still the first sixth of the
  payload) with the reason in the test.

  Scaffold no-leak and skill-sync green:
  `pytest tests/test_plugin_sync.py tests/test_scaffold_no_leak.py
  tests/test_package_skills.py -q` -> `28 passed`. The generated
  `plugins/claude-code/skills/exomem/SKILL.md` was refreshed with
  `exomem package-skills --plugin-root plugins/claude-code`; the version field
  in `plugin.json` was left at its committed value, which that test excludes
  from comparison because it legitimately differs between a checkout and a
  release build. Wider bootstrap/skill scope:
  `pytest tests/test_bootstrap.py tests/test_epistemic_bootstrap_contract.py
  tests/test_bootstrap_capabilities.py tests/test_record_public_surface.py
  tests/test_install_skill.py tests/test_workflow_skills.py … -q`
  -> `118 passed` with the one re-cut pin above.

## 5. Acceptance (spec round-trip)

- [ ] 5.1 Hookless quiet-loop test over a REAL registered family (e.g.
  `scope_divergence_semantic`): quiet with reason lands through
  `triage_memory`, persists across a fresh engine, is listed with origin in the
  dispositions view, resets to `normal` — with every envelope class disposition
  unchanged throughout; plus a pin that the hookless custom-instructions block
  and compact bootstrap name the family-disposition surface and the vocabulary
  discovery path rather than a hardcoded family table.
- [ ] 5.2 `openspec validate --all --strict` green; lean scoped suites green;
  full-suite at the completion boundary with failures attributed against the
  origin/main baseline.
