## 1. Contract and red-first acceptance

- [ ] 1.1 Add the `agent-bootstrap-contract` delta covering the commitments, the shipped
  vocabulary, the capture nudge, the recipes, and the every-tier reach requirement.
- [ ] 1.2 Add `tests/test_epistemic_bootstrap_contract.py` asserting the payload carries
  an `epistemic_contract` section with five imperative commitments.
- [ ] 1.3 Assert the taught outcome set equals `semantic_units.EPISTEMIC_OUTCOMES` and
  the taught metadata keys equal `semantic_units.GOVERNED_UNIT_METADATA_KEYS`, comparing
  against the constants rather than literals.
- [ ] 1.4 Assert the taught unit kinds for question, hypothesis, and prediction are
  members of `semantic_blocks.BLOCK_TYPES`.
- [ ] 1.5 Assert the commitments cover append-only raw material, supersession over
  overwrite, expectation before answer, categorical judgment with no numeric confidence,
  and refuted-stays-active.
- [ ] 1.6 Assert the capture nudge routes a durable future expectation to a prediction
  unit with a revisit date.
- [ ] 1.7 Assert `note_type_recipes` gains question, hypothesis, and prediction recipes,
  each scoped as material inside a compiled page.
- [ ] 1.8 Assert the section is present on the compact profile and on every profile.
- [ ] 1.9 Assert the commitments survive a reduced `ActiveSurfaceDescriptor`, and that
  the filtered section names no unavailable command.
- [ ] 1.10 Assert the Records `intent_boundary` distinguishes a future-observation claim
  from observed state and planning intent.
- [ ] 1.11 Add the regression pinning the audit's original defect: the payload as a whole
  now teaches append-only, supersession, contradiction, and the outcome vocabulary.
- [ ] 1.12 Run the new file and record the verbatim red output before writing any
  implementation.

## 2. Payload implementation

- [ ] 2.1 Build the `epistemic_contract` section in `op_bootstrap` from
  `semantic_units.EPISTEMIC_OUTCOMES` and `GOVERNED_UNIT_METADATA_KEYS`, keeping the
  commitments free of command names.
- [ ] 2.2 Put command routing in a separate `routes` sub-key so surface filtering
  degrades it without touching a commitment.
- [ ] 2.3 Add the capture-nudge clause.
- [ ] 2.4 Add the question, hypothesis, and prediction recipes to `note_type_recipes`.
- [ ] 2.5 Add the prediction line to the Records `intent_boundary`.
- [ ] 2.6 Mark the deferred due-state extension point with a comment naming the blocking
  dependency; implement no predicate.
- [ ] 2.7 Bump `contract_version` and update the single pinned assertion in
  `tests/test_bootstrap.py`.

## 3. Verification

- [ ] 3.1 Run the new test file green and record the verbatim output.
- [ ] 3.2 Run `tests/test_bootstrap.py`, `tests/test_bootstrap_capabilities.py`, and
  `tests/test_bootstrap_compact_budget.py` green, confirming the compact byte ceiling
  holds without being raised.
- [ ] 3.3 Run `tests/test_scaffold_no_leak.py` green, since `src/exomem/` changed.
- [ ] 3.4 Confirm `tests/fixtures/mcp_tool_schemas.json` and
  `src/exomem/tool_surface_contract.json` are byte-identical to `origin/main`.
- [ ] 3.5 Run `uvx ruff check .`.
- [ ] 3.6 Run `openspec validate teach-epistemic-bootstrap-contract --strict` and
  `openspec validate --specs --strict`.
