## Why

Exomem's client base is two-tier. A skill-capable Claude surface loads the shipped
`SKILL.md` scaffold, which carries the product's whole epistemology across roughly a
thousand lines: raw material is append-only, compiled material is superseded rather
than overwritten, a judgment is categorical and never a number, a refuted claim stays
active, and a durable expectation is written down before its answer arrives. Every
other client — hosted agents, generic MCP clients, anything without the skill — sees
only the `bootstrap` payload. That payload is the entire contract those clients ever
receive.

The payload does not teach any of it. It contains zero occurrences of "append-only",
"immutable", or the word for the discipline itself; two of "supersed", both incidental
routing labels; and one of "contradict", inside a filter example. The loop primitives
shipped in `add-epistemic-loop-primitives` — the `prediction` kind, the governed
`verdict` and `check_by` unit-metadata keys, and the closed five-word outcome
vocabulary — are addressable through the tools but are named nowhere a generic client
will read them.

The consequence is that the product's core behaviour silently bifurcates by client
tier. A skill-equipped agent supersedes a stale conclusion and records a refuted
prediction; a hosted agent, given the same vault and the same user, overwrites the page
and never writes the prediction at all. Two clients against one vault produce two
different epistemologies, and only one of them is the product.

The fix is placement, not volume. The doctrine is short; it has simply never been put
on the path every client actually reads.

## What Changes

- Add an `epistemic_contract` section to the `bootstrap` payload carrying five
  commitments in imperative form: preserve the record, supersede rather than overwrite,
  state the expectation before the answer, judge categorically, and keep the negative
  result.
- Teach the loop vocabulary exactly as it exists in code: the closed five-word outcome
  set drawn from `semantic_units.EPISTEMIC_OUTCOMES`, the governed `verdict` and
  `check_by` unit-metadata keys from `GOVERNED_UNIT_METADATA_KEYS`, the `prediction`,
  `hypothesis`, and `open_question` kinds from `semantic_blocks.BLOCK_TYPES`, and the
  `contradicts` and `supersedes` typed relations.
- Add a capture-nudge clause stating that a durable expectation about a future
  observation is written as a prediction unit with a `check_by` date, rather than left
  in prose or in the assistant's own short-term memory.
- Add `question`, `hypothesis`, and `prediction` recipes to the payload's
  `note_type_recipes`, each marked as a block inside a compiled page rather than a new
  page type.
- Extend the Records `intent_boundary` with the prediction boundary, so a statement
  about a future observation is not misrouted as an observed Record or as Planning
  intent.
- Carry the section on the compact profile, not only on `full`, because compact is what
  a generic client actually calls; and keep the commitments free of tool names so
  surface filtering can never strip the doctrine from a reduced surface.
- Bump the bootstrap `contract_version`, consistent with the existing rule that adding
  a section to the portable contract moves its version.

Deliberately out of scope, and named in `design.md`: deterministic per-vault due-state
counts in the payload (for example "2 predictions past their check date"). Those depend
on audit categories being built in a parallel, unmerged lane; inventing a second
predicate here would create exactly the drift this change exists to remove.

**No tool-surface movement.** This change edits payload data only. No tool docstring,
parameter, or input schema changes, so `tests/fixtures/mcp_tool_schemas.json`,
`src/exomem/tool_surface_contract.json`, and the MCP discovery fingerprint are
untouched. `semantic_authoring.AUTHORING_CONTRACT_VERSION` is deliberately not bumped;
see `design.md`.

## Capabilities

### New Capabilities

None. This change adds requirements to an existing capability.

### Modified Capabilities

- `agent-bootstrap-contract`: the portable contract teaches the epistemic commitments,
  the loop vocabulary, and the capture nudge to every client tier, on the compact
  profile, and survives surface filtering on a reduced surface.

## Impact

- Affects `src/exomem/commands.py` `op_bootstrap` payload data and the pinned
  `contract_version` constant in `tests/test_bootstrap.py`.
- Adds `tests/test_epistemic_bootstrap_contract.py`.
- Consumes part of the compact-payload byte budget pinned by
  `tests/test_bootstrap_compact_budget.py`; the section is written dense for that
  reason and the budget gate stays green.
- Introduces no tool, argument, index, model call, migration, or ranking change, and
  inspects no vault content. Reading bootstrap still writes nothing.
