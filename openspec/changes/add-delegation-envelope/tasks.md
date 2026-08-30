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

- [ ] 3.1 Quiet-offer derivation from durable manual-origin dismissal records
  (event count, monotonic, automatic origin excluded): third event arms exactly
  one offer; `quiet_offered_at` recorded; cleared only by explicit family reset
  to `normal`; a decline without reset never re-offers. Red-first through the
  review-state store, incl. a records-not-live-index pin (deleting the
  dismissed items does not disarm the offer) and a no-usage-signals structural
  pin (the derivation reads review-state records only).
- [ ] 3.2 The offer changes nothing by itself: disposition unchanged until an
  explicit decision lands. Red-first.
- [ ] 3.3 Review-state schema migration: dismissal records gain family
  attribution and the family slate gains a durable `quiet_offered_at` slot
  that survives a `normal` disposition; one version bump, previous-schema
  files migrated on load and rewritten on next write, newer schema refused by
  an older runtime with a named error. Red-first on all three behaviours over
  fixture files at both versions.

## 4. The dispositions view and the agent contract (design D5; command-surface delta)

- [ ] 4.1 `review_memory(mode="dispositions")` gains the structurally separate
  envelope block (class, ceiling, disposition/marker, provenance). If the
  recorded response contract or packaged digest moves, follow the documented
  two-phase rollout and record it here. Red-first pin on the two-block shape.
- [ ] 4.2 Bootstrap + scaffold/plugin SKILL.md copies + hookless
  custom-instructions block teach the decider protocol and how to discover the
  registered family vocabulary; ≤ 50 lines per carrier, counted by test;
  compact payload measured before/after and recorded here within the
  `COMPACT_BYTE_CEILING` discipline. Scaffold no-leak and skill-sync tests
  stay green.

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
