## Context

Two shipped primitives already describe the intent/observation binding and are already validated, round-tripped, and governance-projected:

- A Planning item may carry at most 16 `progress_evidence` mappings of exactly `{collection, role, view}` — an opaque `exomem://memory/<uuid>` Records-collection reference, `progress` or `completion`, and a saved-view name (`planning.py`, `_validate_evidence`).
- A Records manifest may carry `links.plans`, a bounded list of `PlanLink(reference, query)` (`structured_collections.py`, `PlanLink` / `_parse_links`), projected opaquely by `record_governance._project_plan_link`.

Both deliveries deliberately stopped short of evaluation. The Planning capability states that "inline Records query descriptors and governed target evaluation SHALL remain deferred to planned-versus-recorded Review", and the `records` capability states "planned-versus-recorded comparison is outside this delivery". This change is that deferred Review, and it is a basic slice: it consumes only what already ships.

The reading side is equally shipped. `planning.query` runs the bounded evaluator over authorized Planning files and returns an egress-projected payload. `record_governance.resolve_collection` resolves only fully released manifests and treats every other case as absent. `record_governance.query_collection` authorizes the manifest, its canonical source, and the named saved view before `record_formats` parses anything, then `record_governance.project_query_result` reconstructs a default-deny wire envelope. The review is composition, not new machinery.

The core repo constraint applies with unusual force here: the server measures, the brain reasons. A "plan progress" surface is exactly where a percentage, a health verdict, or a ranked worry list would feel helpful and would be wrong. This design refuses all three.

## Goals / Non-Goals

**Goals:**

- Make "did the thing we planned produce the outcomes we recorded" answerable in one read-only call over the shipped primitives.
- Execute each bound Records saved view through the existing governed query path, with authorization preceding resolution on the cross-profile hop.
- Present authored intent and observed counts side by side, with exact numbers and explicit truncation.
- Report an unavailable evidence target with a bounded reason that gives missing and withheld targets identical treatment.
- Keep the whole review bounded: capped items, capped distinct query executions, deduplicated `(collection, view)` pairs.

**Non-Goals:**

- Never set, suggest, or derive `health`. The item's authored `health` is echoed unchanged and nothing else about health is produced.
- Never mutate a plan, a record, a manifest, review state, or any other vault file. The review is byte-neutral.
- Never compute a score, ratio, percentage, completion estimate, ranking, or ordering by "worst". Divergence is presented as exact counts and left to human triage.
- No new query grammar, filter operator, saved-view feature, adapter, index, cache, or persisted artifact.
- No Epistemic Inbox integration: no attention category, no review ref, no fingerprint, no snooze/dismiss, no `triage_memory` route.
- No reverse traversal from `links.plans` in this slice; the Planning-side `progress_evidence` descriptor is the single authoritative direction.
- Planning items stay outside ordinary recall and the graph. Unchanged.

## Decisions

**Standalone mode, not an attention category.** The outline left this open. An attention category would mint `exomem://review/<id>` refs bound to signal fingerprints, which exist so a reviewer can *decide* — snooze, dismiss, accept. Plan-progress has nothing to decide: divergence between an authored plan and its recorded evidence is a fact the human interprets, not a finding the system proposes and the human disposes of. Putting it in the ranked Inbox would also require a rank, and ranking "which plan is furthest off" is exactly the score this change forbids. It is therefore a standalone read-only mode, and the `attention-queue` delta records that as a guarantee rather than leaving it implied.

**Selection is Planning-side and exact.** Reviewed items are `lifecycle: active`, `status: active`, `commitment: committed`, and carry a non-empty `progress_evidence` list. Status and commitment are pushed into `planning.query` as exact filters through the shipped grammar; the `progress_evidence` presence test happens in Python because a Planning manifest need not declare that optional field, and filtering on an undeclared column would refuse the whole collection instead of returning zero items.

**Counts, not rows.** Each executed evidence query contributes `matched`, `returned`, `truncated`, the saved view's own `aggregate` when it declares one, and the snapshot hash — never record rows or bodies. This keeps the response bounded independently of how many records exist, and it keeps a cross-profile review from becoming a bulk Records disclosure channel by another name.

**Divergence is a count block.** Per item the review reports `evidence_bindings`, `resolved_bindings`, `unresolved_bindings`, `progress_bindings`, `completion_bindings`, `progress_observations`, and `completion_observations`. Every value is a non-negative integer. Zero completion observations against a committed active item is the interesting signal, and it is expressed as the integer `0` rather than as a flag, a colour, or a verdict.

**Deterministic order, no ranking.** Items are ordered by `(collection_id, plan_id)` and evidence within an item keeps its authored descriptor order. Ordering carries no meaning, which is the point: nothing in the response implies that the first item is the one to worry about.

**Budgeted execution with deduplication.** Distinct `(collection reference, view)` pairs are executed once per call and reused across items. A per-call cap on distinct executions (default 64) bounds worst-case cost at 25 items x 16 descriptors; exceeding it sets `bindings_truncated` and marks the unexecuted bindings `budget_exhausted` rather than silently dropping them.

**Bounded, non-disclosing unavailability reasons.** An evidence binding that cannot be executed is reported as `collection_unavailable`, `profile_mismatch`, `view_unavailable`, `query_unavailable`, `result_withheld`, or `budget_exhausted`. Missing and withheld collections both produce `collection_unavailable`, matching the parity rule the Planning relation envelope already established, so the review cannot be used to probe for hidden collections. A whole Planning collection that fails to resolve or query increments `collections_unavailable` and discloses nothing further.

**Composition instead of a new egress projector.** The other read-only review modes (`relation-queue`, `adoption`, attention) register no projector; they compose already-governed pieces. This mode does the same: the Planning side arrives already projected by `planning_query`, and the Records side is read only after `project_query_result` returns a non-withheld envelope. A withheld envelope becomes `result_withheld` and contributes no numbers.

**The pinned tool surface is not moved here.** `review_memory`'s `mode` docstring is the source of the pinned MCP schema fixture and the packaged tool-surface digest. This change implements and wires the mode — it works through MCP, REST, and CLI — and updates the runtime `INVALID_MODE` message, but deliberately does not edit the docstring, because that would move `tests/fixtures/mcp_tool_schemas.json` and `src/exomem/tool_surface_contract.json` and force the release-blocking connector-refresh fan-out. That documentation step is listed as a deferred task and belongs to that fan-out.

## Risks / Trade-offs

- **Cost of a wide review.** Each distinct evidence view parses its Records source. Mitigation: capped items, capped distinct executions, `(collection, view)` deduplication, and counts-only extraction. Worst case is bounded and reported.
- **An undocumented mode is discoverable only from the CLI/REST call or the spec until the pinned schema is refreshed.** Accepted deliberately; the alternative is touching a release-blocking artifact from a feature change. Recorded in the proposal impact and in tasks.
- **Counts-only can feel thin.** A reviewer may want the matching records. That is `record_memory(action="query")` with the same view, one call away, and keeping it out of this response is what keeps the review bounded and non-disclosing.
- **Selection excludes `blocked` and `planned` items.** The outline says active committed items, and a narrow first slice that answers the question exactly is better than a broad one that invites the system to opine on which state deserves attention. Widening selection later is additive.
- **Divergence counts invite an obvious ratio.** Anyone can divide `completion_observations` by something. The guarantee is only that the server never does, so no derived judgment is ever attributable to Exomem. The tests assert that no float and no score-shaped key appears in the payload.
