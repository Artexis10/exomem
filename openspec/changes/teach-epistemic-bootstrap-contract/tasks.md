## 1. Contract and red-first acceptance

- [x] 1.1 Add the `agent-bootstrap-contract` delta covering the commitments, the shipped
  vocabulary, the capture nudge, the recipes, and the every-tier reach requirement.
- [x] 1.2 Add `tests/test_epistemic_bootstrap_contract.py` asserting the payload carries
  an `epistemic_contract` section with five imperative commitments.
- [x] 1.3 Assert the taught outcome set equals `semantic_units.EPISTEMIC_OUTCOMES` and
  the taught metadata keys equal `semantic_units.GOVERNED_UNIT_METADATA_KEYS`, comparing
  against the constants rather than literals.
- [x] 1.4 Assert the taught unit kinds for question, hypothesis, and prediction are
  members of `semantic_blocks.BLOCK_TYPES`.
- [x] 1.5 Assert the commitments cover append-only raw material, supersession over
  overwrite, expectation before answer, categorical judgment with no numeric confidence,
  and refuted-stays-active.
- [x] 1.6 Assert the capture nudge routes a durable future expectation to a prediction
  unit with a revisit date.
- [x] 1.7 Assert `note_type_recipes` gains question, hypothesis, and prediction recipes,
  each scoped as material inside a compiled page.
- [x] 1.8 Assert the section is present on the compact profile and on every profile.
- [x] 1.9 Assert the commitments survive a reduced `ActiveSurfaceDescriptor`, and that
  the filtered section names no unavailable command.
- [x] 1.9a Parametrise the same assertion over every profile in
  `commands.PRODUCT_SURFACE_PROFILES` via `hosted_gateway.hosted_agent_surface_descriptor`,
  so narrowing a shipped hosted profile cannot regress the doctrine with the suite green.
- [x] 1.10 Assert the Records `intent_boundary` distinguishes a future-observation claim
  from observed state and planning intent.
- [x] 1.11 Add the keyword regression against the *measured* `origin/main` baseline, not
  against zero: pin the pre-change occurrence counts and assert each term strictly
  exceeds its own baseline, so a revert fails rather than coasting on words the payload
  already contained. Exclude `immutable`, which was zero before and stays zero.
- [x] 1.12 Run the new file and record the verbatim red output before writing any
  implementation.

## 2. Payload implementation

- [x] 2.1 Build the `epistemic_contract` section in `op_bootstrap` from
  `semantic_units.EPISTEMIC_OUTCOMES` and `GOVERNED_UNIT_METADATA_KEYS`, keeping the
  commitments free of command names.
- [x] 2.2 Carry no routing inside the section; leave `tool_defaults` and
  `authoring_contract.route_by_intent` as the single place a command is named.
- [x] 2.3 Add the capture-nudge clause.
- [x] 2.4 Add the question, hypothesis, and prediction recipes to `note_type_recipes`.
- [x] 2.5 Add the prediction line to the Records `intent_boundary`.
- [x] 2.6 Mark the deferred due-state extension point with a comment naming the blocking
  dependency; implement no predicate.
- [x] 2.7 Bump `contract_version` and update the single pinned assertion in
  `tests/test_bootstrap.py`.

## 3. Verification

- [x] 3.1 Run the new test file green and record the verbatim output.
- [x] 3.2 Run `tests/test_bootstrap.py`, `tests/test_bootstrap_capabilities.py`, and
  `tests/test_bootstrap_compact_budget.py` green. Trim the doctrine to fit the compact
  ceiling first; only if a genuine margin is unreachable without dropping a commitment
  or a recipe, raise `COMPACT_BYTE_CEILING` and record the decision in its comment.
- [x] 3.3 Run `tests/test_scaffold_no_leak.py` green, since `src/exomem/` changed.
- [x] 3.4 Confirm `tests/fixtures/mcp_tool_schemas.json` and
  `src/exomem/tool_surface_contract.json` are byte-identical to `origin/main`.
- [x] 3.5 Run `uvx ruff check .`.
- [x] 3.6 Run `openspec validate teach-epistemic-bootstrap-contract --strict` and
  `openspec validate --specs --strict`.
