## Why

An agent cannot correct one claim inside an entity page. Every structured path refuses:
`add_observation` refuses typed links on a compact unit (`COMPACT_RELATIONS_REQUIRE_RICH_KIND`),
refuses `as_of` on a compact unit (`COMPACT_METADATA_REQUIRES_RICH_KIND`) and refuses entity
pages outright (`OBSERVE_TARGET_NOT_WRITABLE_COMPILED_PAGE`). `supersede_note` works only on whole
pages. The stale claim often sits in facet prose with no unit identity, so nothing can name it.
The correction then lands as untyped prose that neither search nor the graph can see.

Every note is a hub with its own small graph of units. A correction to one claim in a hub must be a
first-class, machine-visible supersession in that same note. It must not be a new page or prose.

The read side already exists. A rich unit with `relations: supersedes: [[#anchor]]` marks its
same-page target as superseded in recall, topic briefs and the `currency` dimension. This change
adds the write side and the missing reads.

## What Changes

- Add a `supersede` operation to `observe_memory`, exposed as `edit_note` action
  `supersede_observation`. It takes the page, the target unit (a unit id, or an exact text span),
  the replacement text, `as_of`, `why` and optional links. In one governed write it appends the
  replacement as a rich unit that carries `supersedes: [[#<target>]]`, gives the target an authored
  anchor if it has none, and records `why` in the page's revision history. It rewrites no other
  unit.
- Add the other claim-level intents on the same target resolution. `assess_observation` verifies or
  refutes one claim: it sets the target's verdict from the registry's verdict set and adds evidence
  links, and leaves the text unchanged. `link_observation` adds typed links to one claim and leaves
  the text unchanged. Together with `supersede_observation` they are the claim-level operation
  family that `make-vault-store-canonical` D10 names, built on today's unit identity, so they port
  to store mode unchanged.
- Let `observe_memory` add, update, remove and supersede units on pages whose note-type role is
  `entity`. A compact add is a server-side append to `## Observations` that needs no
  `expected_hash`. Entity pages stay outside note-type role `compiled`.
- Add a governed `correction` kind to the core kinds pack, carrying a new `replacement` kind trait
  that tells a supersede which kind to use when the caller names none. The rich-kind refusals name the valid
  kinds, and they name `kind` when the caller passed a kind key as `category`.
- `read_page(part="units")` shows each unit's `as_of`, its `supersedes` targets and its derived
  `superseded_by`. The `currency` dimension on `search_memory` includes same-page unit supersession.
- Add the audit check `entity_facet_unaddressed`. It lists facet list items on entity pages that
  carry no unit identity. Its fix converts each one in place to a compact unit through the
  previewed maintenance plan, so later corrections have a target.
- The relation census stops counting same-page `replaces` edges as `same_page_epistemic`, and the
  `supersedes` teach line names units as well as pages.

## Capabilities

### Modified Capabilities

- `semantic-write-contract`: unit-level supersession, entity observation targets, the correction
  kind and kind-listing refusals, and the supersession view of units.
- `action-first-audit`: the entity facet addressability check and its previewed fix.

## Impact

- Code: `observe_memory.py`, `public_tools.py` (`edit_note`), `commands.py` (`page_units`),
  `audit.py`, `audit_fix.py`, `relation_census.py`, `vocabulary/packs/core/kinds.yaml` and
  `relations.yaml`.
- Tool surface: one new `edit_note` action, which changes the tool-surface fingerprint and may
  raise the `edit_note` schema ceiling.
- Docs: scaffold references for supersession and semantic authoring, the generated plugin trees
  and capability docs.
- Stored data: an entity page gains appended units and anchors only through an explicit write or
  an applied maintenance plan.
