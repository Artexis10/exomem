## Why

Planning is epistemically orphaned. No field connects a plan to the knowledge
that motivates it, so "why does this initiative exist" is unanswerable from
the system, and when a motivating belief is superseded nothing can surface the
plans premised on it — stale plans survive contrary evidence exactly the way
stale beliefs are prevented from doing.

## What Changes

- Add an optional `motivation` field to Planning items: a bounded list (≤16)
  of `exomem://memory/` references, validated the same way `progress_evidence`
  is validated — shape-checked, ref-format-checked, and refused when
  malformed, without resolving the target or inferring anything from it.
- Support filtering Planning queries by a motivating reference through the
  existing generic `filters` mechanism (`column: motivation`), so `plan_memory`
  callers can select the plans premised on a given piece of knowledge.
- Explicit non-goal: Planning items remain outside recall and the graph.
  `motivation` is a reference from a plan to knowledge, never the reverse; it
  creates no relation-graph edge and never makes a plan appear as a memory
  hit. Plans cite knowledge, never the reverse.

## Capabilities

### New Capabilities

- `planning`: no `openspec/specs/planning/` capability exists under
  `openspec/specs/` yet, so this delta declares a new capability. Note that
  `add-multi-horizon-planning` also declares `planning` and is complete but
  unarchived; the two deltas share no requirement names, so archive order is
  safe. This delta is scoped to the `motivation` field and its query filter.

### Modified Capabilities

None.

## Impact

- `src/exomem/planning.py`: new `_validate_motivation` shape validator, wired
  into `_validate_optional`; `motivation` added to `_OPTIONAL` so it can be
  deleted through `update()` like every other optional field.
- No change to `src/exomem/plan_memory.py`, `tests/fixtures/mcp_tool_schemas.json`,
  or `src/exomem/tool_surface_contract.json` — the field rides the existing
  `item`/`changes` dicts and the existing generic `filters` list, so the
  pinned MCP tool surface does not move.
- New focused tests in `tests/test_planning_motivation.py` covering shape
  validation, round-trip, the query filter, and the relation-graph non-goal.
- Additive and optional: absence of `motivation` behaves exactly as before
  this change. The governed ref-list shape is enforced only where the manifest
  declares `motivation` as an array, so a vault that had already declared its
  own `motivation` field of another type keeps reading, querying and mutating
  exactly as before rather than being refused on every normalized record.
