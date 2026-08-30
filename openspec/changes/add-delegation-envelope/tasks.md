# Tasks — add the delegation envelope

Every test lands red first (verbatim failing output recorded before the
implementation, then green). Measured budgets are written into this file when
the task is ticked, not estimated.

## 1. Envelope core (design D1–D3)

- [ ] 1.1 `src/exomem/envelope.py` (import-cheap, torch-free): the closed class
  enum; per-class ranges incl. the `confirm-shortcut` definition; ceilings;
  `derive_envelope(level)` pinned by test against the design table; write-time
  refusals for unknown class, out-of-range disposition, and `disclosure`;
  read-time report-and-ignore for unknown stored ids (red-first: a config with
  a future class id still serves and names it).
- [ ] 1.2 Shared-config storage: `envelope` object in `mode.config_path()`;
  overrides persist across restarts and prominence changes; reset per class;
  absent key = derivation (rollback pin). Red-first over a temp config root
  (no real state root — fixture discipline per `tests/conftest.py`).
- [ ] 1.3 Serve the envelope: bootstrap engagement block carries every class
  with ceiling, disposition or governance-owned marker, and
  fixed/derived/override provenance. Red-first pin on the payload.

## 2. Ceiling enforcement (design D1, D6)

- [ ] 2.1 Tiered confirm-required test: with the most permissive envelope and
  `maximal` prominence — (a) the served envelope still marks
  `restructure_execution` confirm-required, (b) `op_delete` still requires its
  explicit `confirm` parameter, (c) the adoption apply surface still defaults
  preview-first. Mechanism-removal proof: a scratch mutant that lets the
  envelope override the served confirm-required marker fails (a). No new
  server-side confirm parameters; the served contract names the
  supersession/entity-creation gap as future work.
- [ ] 2.2 Founder-gate refusal is the sole error for `restructure_execution`:
  any non-confirm disposition request refuses naming the gate; the generic
  range refusal never fires for this class. Red-first both halves.

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
