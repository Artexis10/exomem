# Tasks — add the delegation envelope

Every test lands red first (verbatim failing output recorded before the
implementation, then green). Measured budgets are written into this file when
the task is ticked, not estimated.

## 1. Envelope core (design D1–D3)

- [ ] 1.1 `src/exomem/envelope.py` (import-cheap, torch-free): the closed class
  enum, per-class ranges, ceilings, `derive_envelope(level)` pinned by test
  against the design table; refusal errors for unknown class, out-of-range
  disposition, and `disclosure`.
- [ ] 1.2 Shared-config storage: `envelope` object in `mode.config_path()`;
  overrides persist across restarts and prominence changes; reset per class.
  Red-first tests over a temp config root (no real state root — fixture
  discipline per `tests/conftest.py`).
- [ ] 1.3 Serve the envelope: bootstrap engagement block carries every class,
  disposition, and derived/override marker; the dispositions view lists the
  envelope beside family dispositions. Red-first pins on both surfaces.

## 2. Ceiling enforcement (design D1, D6)

- [ ] 2.1 Confirmation invariant test over the four confirm-required surfaces
  (restructure apply, supersession commit, entity creation, deletion): with the
  most permissive envelope and `maximal` prominence, each still requires the
  explicit confirmation its surface defines today. Mechanism-removal proof: a
  scratch mutant that consults the envelope above the ceiling fails the test.
- [ ] 2.2 Standing-delegation refusal: any non-confirm disposition request for
  `restructure_execution` refuses naming the founder gate. Red-first.

## 3. Adaptation (design D4)

- [ ] 3.1 Quiet-offer derivation from existing review-state dismissal records:
  third dismissal arms exactly one offer; `quiet_offered_at` recorded on the
  family; no repeat until the family resets to `normal`. Red-first through the
  review-state store, plus a no-usage-signals pin (the derivation function's
  inputs are review-state records only — asserted structurally, not by prose).
- [ ] 3.2 The offer changes nothing by itself: disposition unchanged until an
  explicit decision lands. Red-first.

## 4. The agent contract (design D5)

- [ ] 4.1 Bootstrap + scaffold/plugin SKILL.md copies + hookless
  custom-instructions block teach the decider protocol; ≤ 50 lines total
  across carriers, counted by test; compact payload measured before/after and
  recorded here; `COMPACT_BYTE_CEILING` respected without being raised past
  its current headroom discipline.
- [ ] 4.2 Scaffold no-leak and skill-sync tests stay green.

## 5. Acceptance (spec round-trip)

- [ ] 5.1 Hookless round-trip test: a scripted hookless session issues the
  plain-language quiet request; assert the named family is quiet with reason
  and origin, other families untouched, persistence across a fresh engine, and
  reset. Uses the installed CLI journey pattern only if a deterministic
  in-process equivalent cannot express it; otherwise in-process.
- [ ] 5.2 `openspec validate --all --strict` green; lean scoped suites green;
  full-suite at the completion boundary with failures attributed against the
  origin/main baseline.
