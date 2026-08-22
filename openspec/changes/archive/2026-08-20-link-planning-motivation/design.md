## Context

Exomem's epistemic architecture audit (2026-08-14, §13, Tier 2 item 6) named
Planning as epistemically orphaned: `src/exomem/planning.py` already models
areas, outcomes, initiatives, and work items with rich lifecycle/hierarchy
semantics and two existing reference-bearing optional fields —
`progress_evidence` (bounded list of `{collection, role, view}` descriptors
pointing at Records) and `execution` (bounded list of `{kind, ref, label?}`
pointers to external systems) — but nothing connects a plan to the *belief*
that justifies its existence. When a Note is superseded, nothing in the
system can answer "which plans were premised on this."

`memory_refs.py` already defines the stable `exomem://memory/<uuid>` reference
namespace used throughout the rest of the product (Records' `progress_evidence`
validates its `collection` field the same way: `memory_refs.parse_memory_ref(...)
is None` refuses malformed values). `motivation` reuses that exact primitive
rather than inventing a new reference shape.

Planning items are already structurally outside recall and the graph — not by
per-field convention but by path: `recall_policy.is_structured_only_path`
excludes every raw Planning item, and `index_sync` only ever pushes the
collection *manifest* into the lexical/vector/graph indexers, never a raw
item file (`tests/test_planning_recall.py` locks this down at 1,000-item
scale). `motivation` lives inside a raw item's frontmatter, so it inherits
that exclusion for free — no new enforcement point is required to keep plans
out of recall.

## Goals / Non-Goals

**Goals:**

- Let a Planning item declare which memory (Notes, Entities, Evidence — any
  `exomem://memory/...`-addressable page) motivated it, with the same shape
  discipline as `progress_evidence`: bounded list, validated ref format,
  refused when malformed, optional and defaultless.
- Let a `plan_memory query` caller select the plans premised on a given piece
  of knowledge, so a superseded belief's dependent plans become discoverable.
- Keep the change purely additive: no existing Planning item, manifest, or
  caller is invalidated by this field's absence.

**Non-Goals:**

- Planning items do not enter recall or the relation graph through this
  field. `motivation` is a reference from plan to knowledge, never the
  reverse — it must not create a graph edge and must not make a plan appear
  as a memory hit anywhere recall is evaluated.
- No resolution, existence-checking, or dereferencing of the referenced
  memory. Exactly like `progress_evidence`'s `collection` ref, `motivation`
  entries are validated for shape only; a ref to a memory page that does not
  exist (yet, or ever) is not an error here.
- No automatic supersession propagation. This change adds the pointer that
  makes "which plans cite this belief" answerable by query; it does not add
  the reasoning that walks from a superseded Note to its dependent plans.
  That consumption is a separate, later concern.
- No new top-level `plan_memory`/`planning.query()` parameter. See Decision 2.

## Decisions

### 1. Model `motivation` on `progress_evidence`, not as a new pattern

`progress_evidence` is already the precedent for "a bounded list of validated
references living on a Planning item, refused-not-resolved." `motivation` is
simpler than `progress_evidence` (a flat list of ref strings, not
`{collection, role, view}` descriptors) because it needs no role or view —
just "this belief motivates this plan." Reusing
`memory_refs.parse_memory_ref` for the per-entry check and the same 16-entry
bound keeps the validation vocabulary the caller already knows for
`progress_evidence` and `execution`.

Alternative rejected: a single optional string `motivation_ref` instead of a
list. A plan is often premised on more than one belief (e.g. a market
observation Note and a technical feasibility Note together); a bounded list
matches how `progress_evidence` already handles "more than one, but not
unbounded."

### 2. The `motivation_ref=` filter is the existing generic `filters` mechanism, not a new parameter

The approved outline names the query filter `motivation_ref=`. Read literally
as a new top-level keyword argument on `plan_memory`/`planning.query()` (the
way `lifecycle=` is today), it would add a property to `plan_memory`'s
introspected input schema — which is what `tests/fixtures/mcp_tool_schemas.json`
and `src/exomem/tool_surface_contract.json` pin byte-for-byte. Task
instructions are explicit that moving that pinned surface means stopping and
reporting rather than regenerating it.

`planning.query()`'s existing generic `filters` list already supports this
without any signature change: once a collection's manifest declares
`motivation` as a schema field (exactly as it must already declare
`progress_evidence` or `execution` to use them), `filters=[{"column":
"motivation", "op": "contains", "value": "exomem://memory/<uuid>"}]` selects
every item whose `motivation` list contains that reference, through the same
`query_data.evaluate_rows` path every other array-valued Planning field
already uses. This satisfies "the query filter selects the right items"
without moving the pinned tool surface. See the deviation note in the final
report.

### 3. No manifest-schema hardcoding in `planning.py`

Like `progress_evidence`, `execution`, and `tags`, `motivation` is validated
by Planning's own semantic layer (`_validate_optional`) independent of
whether any given collection's manifest declares it as a schema field. A
vault author opts a collection into `motivation` by declaring it in that
collection's `item_schema.fields`, the same way they already opt into
`progress_evidence`. `require_planning_profile`'s fixed core-field check is
untouched.

## Risks / Trade-offs

- **[`motivation_ref=` reads as a missing parameter]** → Documented as
  Decision 2 and called out explicitly as a deviation in the implementation
  report; the generic-filter path delivers the same selection capability
  without moving the pinned MCP surface.
- **[A plan reference smuggled into `motivation` could look like a graph
  edge]** → `_validate_motivation` only accepts `exomem://memory/...` refs
  (via `memory_refs.parse_memory_ref`); an `exomem://plan/...` reference is
  refused the same as any other malformed value, so `motivation` cannot be
  repurposed as an undeclared `parent`/`area` relation.
- **[Future consumers assume `motivation` targets are resolved/valid]** →
  Documented as a non-goal here and mirrors the existing, already-understood
  `progress_evidence` contract; no new expectation is introduced.
