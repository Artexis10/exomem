## Why

`observe_memory` is the natural append tool and the hard stop for a compiled
page it cannot mutate. When the target is an untyped legacy page (frontmatter
present, no compiled `type`), has no frontmatter at all, or lives outside the
governed Knowledge Base root entirely, it refuses with
`OBSERVE_TARGET_NOT_WRITABLE_COMPILED_PAGE` (or `FRONTMATTER_REQUIRED`, or
`OUTSIDE_GOVERNED_ROOT`) and no remediation — the compiled-page code is even
shared with a readonly or excluded access tier, so an agent cannot tell "this
page predates compiled-page typing" apart from "this page is
policy-protected." No adopt/reclassify path upgrades a legacy note in place,
by doctrine: the Knowledge Base is a compiled layer beside the log, not a
migration demand. A dogfood finding (11 Sept 2026) named the gap directly:
no-nudge behaviour should distinguish unsafe semantic mutation from harmless
legacy logging and offer a deterministic safe fallback instead of making the
active agent improvise around schema history.

## What Changes

- `observe_memory` keeps its existing refusal codes (compatibility) and adds,
  for the three legacy-schema causes (untyped page, frontmatterless page, page
  outside the Knowledge Base root), a `remediation` sentence plus a structured
  `resolution` naming the safe next step: append in place via `edit_memory`
  where the surface has body edits, or create a dated child page linked
  `part_of` the legacy page, with a concrete, de-collided `suggested_path`.
  The `remediation` sentence is built from that same `resolution` and
  interpolates its exact `suggested_path` and `part_of` target, because that
  flattened sentence is the only form of this information a caller receives
  today (no MCP structured envelope for this error family — see Design D4).
  Migration into a compiled type stays available only on request
  (`adoption_studio`).
- A readonly/excluded (or otherwise non-read-write) access tier keeps the same
  code but gets a different, tier-naming `remediation` and no `resolution` —
  there is no safe next step to suggest against a policy-protected page.
- No widening of the compiled-page type set: `entity` stays structural, not
  compiled, matching `semantic-write-contract`'s existing `COMPILED_TYPES`
  enumeration.
- One paragraph in the generic scaffold's `references/write-scope.md` documents
  the routing rule. The hosted `SKILL.md` sentence named in the originating
  packet is NOT included in this change — see Design for why.

## Impact

- Affected code: `src/exomem/observe_memory.py` (the three refusal sites: the
  compiled-page check, and the `FRONTMATTER_REQUIRED`/`OUTSIDE_GOVERNED_ROOT`
  branches of its `edit.EditError` handler — no `edit.py` change),
  `src/exomem/_scaffold/_Schema/references/write-scope.md` (+ its
  skill-contract stamp).
- Affected specs: `semantic-write-contract` (adds one requirement; no existing
  requirement changes).
- No error code is renamed or added. No new note kind. No change to hosted
  profile membership or any tool schema/description (verified: the tool-surface
  digest does not move).
