## Why

Planning already stores authored intent, Records already stores observed state, and a Planning item may already carry up to sixteen `progress_evidence` descriptors naming a Records collection, a role, and a saved view. Records manifests may already carry the reciprocal opaque `links.plans` descriptor. Both sides validate and round-trip those bindings, and both deliberately stop there: nothing in the product ever runs the bound query and shows the human what the plan intended next to what the vault actually recorded.

That leaves the closing beat of the programme unanswerable. A user can ask "what did I plan?" and "what happened?" separately, but not "did the thing we planned produce the outcomes we recorded?" — which is the question the two profiles exist to make answerable together. The primitives ship and simply have no consumer, so this change adds the consumer and nothing else.

## What Changes

- Add `review_memory(mode="plan-progress")`: a read-only, cross-profile review that selects active committed Planning items carrying `progress_evidence`, executes each bound Records saved view, and presents authored intent next to observed counts.
- Execute bound evidence through the shipped primitives only — the Planning bounded query evaluator, `record_governance.resolve_collection`, `record_governance.query_collection`, and the default-deny `project_query_result` envelope. No new query grammar, adapter, index, or storage.
- Apply the authorization-precedes-resolution rule the Records delivery established to the cross-profile hop: every evidence target is authorized before it is resolved and before its canonical source is parsed, and a withheld target is reported with the same bounded reason as an absent one.
- Bound the whole review explicitly: a capped number of reviewed items, a capped number of distinct evidence-query executions per call, deduplication of repeated `(collection, view)` pairs, and explicit truncation counters rather than a silently partial answer.
- Present divergence as exact numbers only — binding counts and matched-observation counts per role, plus the authored intent fields — and leave every judgment to the human or the calling agent.
- Enforce the hard non-goals in code and in tests: the review never writes `health`, never mutates a plan or a record, never computes a score, ratio, percentage, or ranking, and never enqueues an Epistemic Inbox item.

## Capabilities

### New Capabilities

- `planning`: Planned-versus-recorded review over shipped primitives — item selection, bound evidence execution, the presented response shape, and the non-adjudication guarantees.

### Modified Capabilities

- `records`: Lift the "planned-versus-recorded comparison is outside this delivery" deferral from the neutral-view requirement and state the read-only cross-profile review contract that replaces it, including bounded evidence execution under authorization-precedes-resolution.
- `attention-queue`: Record the adjudication that plan-progress review is a standalone read-only mode rather than an attention category, so it never enters the ranked Inbox, never mints review refs or fingerprints, and never becomes triageable.

## Impact

- New `src/exomem/plan_progress.py` review module and one new `plan-progress` branch in `commands.op_review_memory`, reaching MCP, REST, and CLI through the existing generated command with no signature change.
- New focused unit tests for the pure selection/divergence logic and new integration tests over a real two-profile vault, including a byte-identical-vault assertion that proves the review writes nothing.
- No new dependency, model, index, database, or migration. Planning items remain outside recall and the graph. Records and Planning canonical files, references, receipts, and response shapes are unchanged.
- Known follow-up, deliberately excluded here: the `review_memory` docstring that documents the mode list is the source of the pinned MCP tool schema (`tests/fixtures/mcp_tool_schemas.json`, `src/exomem/tool_surface_contract.json`). Documenting `plan-progress` there moves the pinned tool surface and requires the separate release-blocking connector-refresh fan-out.
