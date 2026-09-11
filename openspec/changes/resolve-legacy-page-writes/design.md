## Context

`observe_memory` mutates one semantic unit on a compiled page. It refuses a
target that is not a writable compiled page with
`OBSERVE_TARGET_NOT_WRITABLE_COMPILED_PAGE`; separately refuses a
frontmatterless page earlier, from `edit.load_editable`, with
`FRONTMATTER_REQUIRED`; and separately refuses a page whose real file lives
outside the governed Knowledge Base root, also from `edit.load_editable`
(via `edit._resolve`/`kbdir.kb_page_target`), with `OUTSIDE_GOVERNED_ROOT`.
None of the three previously said what to do next, and the compiled-page
refusal collapses two unrelated causes — "wrong/missing type" and
"policy-protected access tier" — into one code with no way to tell them
apart.

## Goals / Non-Goals

**Goals:**
- Keep every existing error code unchanged (compatibility).
- Give all three legacy-schema causes (untyped page, frontmatterless page,
  page outside the Knowledge Base root) a concrete, structured next step
  instead of a dead end.
- Keep the tier-protected refusal visibly different (remediation text only,
  no resolution) so an agent never treats "policy forbids this" as something
  it can route around.
- Make the flattened string an MCP agent actually receives today (see D4)
  carry the concrete next step — the exact `suggested_path` and `part_of`
  target — not merely a pointer at a `resolution` object the agent cannot see
  as structured JSON.
- Advise a `suggested_path` that page creation would actually produce, not one
  a pre-existing same-dated child would silently collide with.

**Non-Goals:**
- No in-place synthesis of frontmatter or a compiled type. `load_editable`
  deliberately "refuses to synthesize it," and the Knowledge Base doctrine is
  that legacy pages are grandfathered, not silently upgraded.
- No widening of `_COMPILED_PAGE_TYPES` / `semantic-write-contract`'s
  `COMPILED_TYPES`. `entity` is deliberately structural
  (`semantic_writes._existing_applicability` returns `"structural"` for it);
  admitting more types into the compiled set is its own, separate spec-delta
  question.
- No adopt/reclassify path that upgrades a legacy note in place. Migration
  stays available only on explicit request through `adoption_studio`.

## Decisions

### D1. Keep the existing codes; attach remediation + resolution instead of minting new ones

A caller (human or agent) that already branches on
`OBSERVE_TARGET_NOT_WRITABLE_COMPILED_PAGE` or `FRONTMATTER_REQUIRED` keeps
working unchanged. The new information rides beside the code as an additive
field, mirroring the existing `COMPACT_METADATA_REQUIRES_RICH_KIND`
precedent in `observe_memory.py` for how a one-sentence `remediation` is
attached to a refusal.

### D2. Tier vs. type get different remediation, and only type gets a resolution

A page whose type is missing or uncompiled is a **schema-history** problem: a
concrete corrective action exists (log elsewhere, don't force it into the
compiled slot). A page whose access tier is not `read-write` is a
**policy** problem: no corrective action is safe to suggest, because the tier
exists specifically to forbid writes regardless of content. Collapsing them
into one remediation would either advise around a policy boundary or bury the
schema-history advice under tier language that doesn't apply. The tier check
runs first: a page that is both untyped and tier-protected is still, first
and foremost, one the agent cannot act on at all.

### D3. The resolution's `suggested_path` branches on whether the legacy page is under the Knowledge Base root

Exomem only ever authors new pages under `Knowledge Base/` (see
`vault.py`'s "exomem only ever authors under Knowledge Base/"). When the
legacy page already lives there, the dated child colocates in the same
directory. When it does not, the suggested destination is
`Knowledge Base/Notes/Research/<parent-dir-slug>/<date>-<slug>.md`, mirroring
the existing read-only-paths fallback already documented in
`references/write-scope.md` ("offers to compile the finding into a
`Notes/Research/<scope>/` page that links back instead").

**Correction (no longer a gap):** an earlier draft of this change treated the
outside-KB branch as unreachable, because `edit._resolve` (via
`kbdir.kb_page_target`) re-roots every path it is given under
`Knowledge Base/` before checking existence, and a page whose real file lives
outside that root instead raises `edit.EditError(code="OUTSIDE_GOVERNED_ROOT",
...)`. That refusal IS in scope: `observe_memory`'s existing
`except edit.EditError as error:` block already catches it, so it needs no
`edit.py` change — only a third branch beside `FRONTMATTER_REQUIRED` in that
same handler. Both branches derive their `rel_path` from the caller's own
`path` text, normalised the same way `edit._existing_page_outside_kb`
normalises it (strip, backslash to slash, lstrip leading `/`, `.md` suffix) —
one shared normalised form (`_legacy_page_given_form`), then re-rooted under
`Knowledge Base/` for the frontmatterless case (reachable only for a page
`load_editable` already found there) and left as-is for the outside-KB case
(`_existing_page_outside_kb` never returns a hit for a given form that
already starts with the KB prefix, so the unprefixed given form already
names the real, outside-KB page). This is exercised end-to-end through
`commands.op_observe_memory` (`test_outside_kb_page_offers_dated_child_resolution`),
not only at the pure-function level.

Filenames are further de-collided with `vault.unique_path` — the same helper
`note.py`'s creation path uses — so `suggested_path` never advises a name
that already exists; a pre-existing same-dated child shifts the advice to
the `-2` form (or further), exactly as an actual creation call would land.

### D4. The structured `resolution` field does not currently reach the MCP client as JSON

Traced through `commands.py` → `command_surface.bind_vault`'s wrapper →
`cli_ops.py`: `observe_memory`'s refusals are `ObserveMemoryError` instances
(a `ValueError` subclass) with no `as_semantic_validation_error()` or
`as_public_dict()` method. `commands.op_observe_memory` flattens them into a
plain `ValueError("CODE: reason Remediation: <text>")`. The MCP wrapper's
structured-envelope path
(`{"success": false, "error": {"code", "message", "remediation", ...}}`) only
fires for `cli_ops.OpError` instances or for an exception chain that reaches a
`.as_semantic_validation_error()` method (`cli_ops.semantic_validation_error_dict`,
walking `__cause__`/`__context__`); anything else — including every
`ObserveMemoryError`-derived refusal today — re-raises and surfaces through
FastMCP's native (unstructured) error path: the flattened string, not a JSON
object. This is true both before and after PR #1137 lands (`gh pr diff 1137`):
that PR changes only the **advertised output schema**
(`register_mcp_tool` in `command_surface.py`, `additionalProperties: true` on
`error`, so a `resolution` key would be schema-permitted) and how `OpError`
gets registered; it does not add routing for a plain "CODE: reason"
`ValueError` family like `observe_memory`'s into the structured envelope.

Given that, this change keeps `resolution` as a real, testable attribute on
`ObserveMemoryError` (verified at both the module layer and the
`commands.op_observe_memory` layer, where it survives on `error.__cause__`)
and builds the `remediation` sentence from that same `resolution` — after
computing it, not before — so the sentence carries the resolution's own
concrete fields: the exact `suggested_path`, the `part_of` target, the
in-place alternative, and the migration pointer, in that order
(`_legacy_routing_remediation`). This is not redundant with the structured
attribute: it is the ONLY form of this information that reaches a caller
today, since `resolution` as a JSON object never crosses the MCP boundary for
this error family (traced above). An earlier draft of this change used a
fixed, generic remediation sentence that pointed at "the suggested dated
child page" without naming it; that left the one channel an MCP agent
actually receives (the flattened `ValueError` string) without the concrete
path or target, defeating the "so an agent never has to improvise" goal for
every consumer that only sees that string. Verified directly: the flattened
string for all three cases (untyped inside KB, frontmatterless, outside KB)
contains the literal `suggested_path` and the literal `part_of` target (see
PR evidence).

Wiring `observe_memory`'s whole error family into the structured MCP envelope
is a separate, materially larger change (it would flip every other
`observe_memory` refusal from unstructured to structured too) and is exactly
the kind of failure-envelope surface PR #1137 is already working; this change
does not attempt it.

### D5. Hosted `SKILL.md` sentence deferred, not shipped

The originating packet asked for one sentence in
`plugins/hosted/skills/exomem/SKILL.md`. Adding it breaks
`tests/test_hosted_plugin_rendering.py::test_v1_hosted_release_identity_fixture_remains_immutable`
and `::test_claude_archive_is_deterministic_and_locked`: that file's bytes
feed a locked `tests/fixtures/hosted/v1-release-identities.json` fixture and
`plugins/hosted/generated/compatibility.json`, both release-owned artifacts
whose regeneration the originating packet's own FORBIDDEN list rules out
("regenerating tool-surface locks/digests/hosted artifacts"). This change
therefore ships only the generic scaffold's `write-scope.md` paragraph; the
hosted sentence is left for a release-owned lane to add alongside its next
scheduled hosted-artifact regeneration.
