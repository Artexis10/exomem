## Why

Exomem can record what was concluded, but it cannot record what was *expected* and then record how that expectation turned out. A hypothesis is authorable today as a rich `## Hypothesis` block; nothing in the governed vocabulary lets an author say "this is a claim about the future", "check this by that date", or "this turned out refuted". The epistemic loop — predict, check, judge — has no primitives, so an agent has to encode it in prose that no filter, hit payload, or review queue can see.

Two failures follow. First, a refuted prediction is indistinguishable from an unexamined one, so negative results silently disappear from recall instead of staying available as the most expensive knowledge a vault owns. Second, the only way an author could approximate "how sure am I" would be a numeric confidence score, which this project has rejected: a stored credence misrepresents the signal and drifts without anyone editing it.

There is also a latent data-loss defect this change must close before it can safely introduce governed unit metadata. `observe_memory`'s update path rebuilds a semantic unit from six fields — kind, category, content, tags, context, relations — and rewrites the unit's Markdown from that reconstruction. Any metadata row it does not know about is dropped. Today that is invisible because no other governed rows exist. The moment `verdict:` exists, an unrelated content edit would silently delete a user's judgment.

## What Changes

- Add `prediction` as a governed semantic-unit kind, alongside the existing `hypothesis`, `result`, and `experiment` kinds. It is a portable core kind with the plural heading alias `Predictions`; it introduces no relation and no page type.
- Add two governed unit-metadata keys with explicit grammar and deterministic validation: `verdict` (a closed categorical enum) and `check_by` (a strict ISO calendar date). Both are rich-form only, because compact observations have no metadata rows.
- Fix the reconstruction defect: `observe_memory` preserves every authored unit-metadata row it does not itself own, so an edit through the six-field path can never silently drop a governed or unknown row. This is the load-bearing correctness assertion of the change.
- Give `observe_memory` governed unit-metadata arguments `verdict`, `check_by`, and `id`, all preserve-by-default on update, with an explicit empty string as the only way to clear one.
- Make the governed metadata keys filterable as `unit.verdict` (closed string) and `unit.check_by` (typed date, ordered comparisons allowed), and surface them on semantic-unit hits, which today omit unit metadata entirely.
- Extend the experiment note-type contract with a `concluded` lifecycle status and an `outcome:` frontmatter field over the same closed vocabulary as `verdict`; reject an invalid or misplaced `outcome` at the frontmatter-write boundary.
- Restate, in the frontmatter spec, that no numeric confidence field exists — now explicitly covering `verdict` and `outcome` as categorical state rather than a credence.
- Teach the shipped skill scaffold (`frontmatter.md`, `page-types.md`) and the public semantic-language guide the new status, the outcome enum, the `prediction` kind, and the two governed unit-metadata keys — including that a refuted result is not a superseded one.

`verdict` is state, not supersession: a refuted prediction keeps `lifecycle: active` and full rank, because refuted is not replaced. Supersession remains reserved for a page whose whole current view is superseded. Nothing in this change gives `verdict` a ranking effect or exempts a unit from page-status inheritance.

**Tool-surface note.** This change moves the pinned MCP tool surface, because `observe_memory` gains three arguments. `tests/fixtures/mcp_tool_schemas.json`, `src/exomem/tool_surface_contract.json`, and `docs/capabilities.md` are regenerated. The MCP discovery fingerprint therefore changes, and the ChatGPT Personal Plugin attestation's pending digest moves with it.

**This change is release-blocking until the ChatGPT Personal Plugin is refreshed and verified against the new surface.** The attestation records the new digest as `pending` with `refresh_required: true` and `rollout_state: awaiting-post-deploy-refresh`; it does not claim the surface registered. Refreshing it is a deliberate release step that requires observing a fresh ChatGPT conversation pick up the new action schema, which cannot be confirmed from the repository. Disconnecting and reconnecting OAuth is not an action-schema refresh. Do not clear the pending state until that verification actually happens.

**Hosted plugin note.** `observe_memory` is a member of the `hosted-alpha-agent-v1` hosted profile, whose committed compatibility descriptor is *derived* from the live command registry, so moving the surface moves that descriptor's identity. This change lets that identity track the surface: a descriptor that stayed pinned while the served schema moved would advertise a contract nobody serves. The profile's command membership, surface fingerprint, skills digest, definition digest, and every package archive are unchanged — only the derived descriptor and the two platform locks that embed its digest move, for both the v1 and v2 candidates. v1 promotion state is `pending`, so no live registration is affected. See `design.md` for the full reasoning and the separately-filed alternative.

## Capabilities

### New Capabilities

- `semantic-unit-language`: The `prediction` governed kind and the `verdict` / `check_by` governed unit-metadata keys, with grammar, deterministic validation, rich-form-only scope, and an explicit prohibition on numeric confidence.
- `semantic-write-contract`: The new kind is recognized by the shared write contract exactly like every other governed kind, and neither the kind nor its metadata participates in any quota, count, or coverage requirement beyond the existing one-unit minimum.
- `structured-retrieval-filters`: `unit.verdict` and `unit.check_by` become closed filterable fields, with `check_by` typed as a date so due-by queries are answerable.
- `semantic-unit-retrieval`: Governed unit metadata appears on semantic-unit hits, with no ranking effect and no change to page-status inheritance.
- `note-type-contract`: The experiment page type gains a `concluded` status and a closed `outcome:` enum, and the frontmatter contract keeps confidence a non-field.

### Modified Capabilities

- `command-surface`: `observe_memory` accepts `verdict`, `check_by`, and `id` as governed unit-metadata arguments and preserves unowned authored metadata rows across a reconstruction.

## Impact

- Affects the semantic-block kind table, the semantic-unit parser and its diagnostics, `observe_memory` reconstruction and round-trip assertion, the structured-filter registry and evaluator, the semantic-unit hit payload and its governed egress projector, the experiment note-type validator, the frontmatter-field write boundary, and the shipped skill scaffold and semantic-language guide.
- The unit serializer gains two keys, so exact unit reads through `read_memory` also report them. Context packs already carry a rich unit's bounded metadata dict, so a judged unit reaches a pack with its verdict without a change there.
- Moves the pinned MCP tool surface and the MCP discovery fingerprint; the ChatGPT Personal Plugin pending attestation digest moves with it and remains release-blocking until that connector is refreshed and verified.
- Moves a frozen-candidate hosted profile's *derived* identity. `hosted-alpha-agent-v1` and `hosted-alpha-agent-v2` both re-render their compatibility descriptor and both platform locks, because those are derived from the live command registry and `observe_memory` belongs to both profiles. This is justified because v1 promotion state is `pending`, so no live registration is invalidated, and because a descriptor pinned against the schema it actually serves would be untrue. Command membership, surface fingerprints, skill digests, definition digests, promotion records, and every package archive are unchanged.
- Introduces no model, no new relation kind, no new page type, no new index, and no ranking change. Every new field is derived from Markdown, which remains canonical.
- Adding `prediction` to the code-owned core kinds means a vault whose semantic-language registry already defines a custom `prediction` kind will report a `canonical_collision` error until that extension is renamed. This is the existing, intended contract for core-kind additions.
