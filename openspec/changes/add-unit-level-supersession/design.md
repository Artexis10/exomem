## Context

Units are compact lines (`- [category] text ^anchor`) or rich blocks (`## <Kind>`, metadata rows,
then content). A rich unit may carry `- relations: supersedes: [[#anchor]]`. The graph resolves a
same-page fragment to a unit-to-unit edge (`epistemic_graph._relation_destination`), and
`working_set_currency.annotate` marks the target `superseded`. Compact units carry no metadata
rows, so they cannot hold an edge. `observe_memory` writes only pages with note-type role
`compiled`, and entity pages have role `entity`.

## Goals / Non-Goals

**Goals:** one call corrects one claim in a hub; the edge is visible to recall, search and the
graph; the history keeps the reason; nothing rewrites another unit.

**Non-goals:** cross-page unit supersession (it works through the same edge already); changing the
compact grammar; automatic application of the facet migration to any vault.

## Decisions

1. **The edge is the only record.** The successor carries `supersedes: [[#target]]`. A reader
   derives `superseded_by` from it. The target unit is not rewritten to point back, so the two
   sides cannot drift. The rejected alternative was a `superseded_by` row on the target: that
   writes two places for one fact, and a compact unit has nowhere to hold it.
2. **The operation is an `edit_note` action, not a new tool.** `edit_note` already targets one
   existing unit and carries `why`. A new tool would add a door that an agent must discover. The
   `supersede_note` description points to the action for a single claim. (Primary source: the
   roster's tool-design row, "Writing effective tools for agents": one obvious operation, errors
   that name the fix.)
3. **A governed `correction` kind, not metadata on compact units.** Relaxing compact units means
   a new compact grammar that every reader parses. A kind is one registry row, and the vocabulary
   rule puts it in the pack. The supersede operation uses the target's kind when the target is
   rich and `correction` when it is compact.
4. **Span targets become units in place.** A span must fall inside exactly one list item that is
   not already a unit. The operation converts that item to `- [<section category>] <text> ^<anchor>`.
   It adds bytes only around the item's unchanged text. The category comes from the item's `##`
   heading, so no list in code decides it. The audit fix uses the same conversion.
5. **Entity admission sits beside role `compiled`, not inside it.** Minimum-unit and compiled
   intent rules keep reading role `compiled`. `observe_memory` admits role `entity` as a unit
   target. A compact add on any admitted page is a server-side append, which already needs no
   `expected_hash`.
6. **The facet check covers every non-unit claim item, not only dated ones.** Exomem has no sound
   date-mention detector, and a month-name list is the word list the C6 rule forbids. Any facet
   claim may need a correction, so every one needs a target. A list item that holds only links,
   or that already parses as a unit or a markdown relation, is excluded by shape.

7. **One intent, one action, one resolver.** Correcting and superseding are one intent: a new
   current view replaces the old one (`supersede_observation`). Verifying and refuting change the
   claim's own settled state (`assess_observation`, a registry verdict plus evidence links).
   Linking adds typed links to the claim (`link_observation`). Assess and link share one leaf
   operation that changes the target's metadata and relations and never its text. All three share
   the target resolver of decision 4. This is the operation half of `make-vault-store-canonical`
   D10 task 5.2; that change owns the unit row table and the one-transaction store write, so this
   change adds no unit table and keeps today's identity rule.
8. **The default replacement kind is a trait, not a literal.** A new `replacement` kind trait
   marks the kind a supersede uses when the caller names none. The core pack gives it to
   `correction`. When no admitted kind, or more than one, carries the trait, the operation asks for
   `kind`. A promoted compact target in assess or link also needs `kind`, and the refusal lists the
   admitted kinds.
9. **Promotion never re-parents other units.** A compact unit that gains metadata becomes a rich
   block appended at the end of the body with the same anchor, and its compact line is removed.
   Rewriting it in place under `## Observations` would put the following compact lines inside the
   new rich block.

## Risks / Trade-offs

- Entity pages gain rich units at the end of the body. Readers that render entity pages already
  render rich units on compiled pages.
- The facet fix touches many lines on a large vault. It runs only through a previewed plan, in
  guarded chunks, with the plan id the owner applies.
- A same-page supersession used to count as a census smell. That count now excludes `replaces`
  edges only.

## Migration

None is automatic. The owner previews and applies `entity_facet_unaddressed` per vault.
