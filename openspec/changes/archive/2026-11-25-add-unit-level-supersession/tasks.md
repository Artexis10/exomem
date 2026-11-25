## 1. Red-first tests for each error path

- [x] 1.1 Red first: `add_observation` on an entity page with category and text refuses today with `OBSERVE_TARGET_NOT_WRITABLE_COMPILED_PAGE`; after the change it appends one compact line to `## Observations` with no `expected_hash` and changes no other line.
- [x] 1.2 Red first: `add_observation` with links and no kind refuses with `COMPACT_RELATIONS_REQUIRE_RICH_KIND`, and the remediation lists the admitted kinds with `correction`; with `category="correction"` it says to pass `kind="correction"`.
- [x] 1.3 Red first: `add_observation` with `as_of` and no kind refuses with `COMPACT_METADATA_REQUIRES_RICH_KIND`, and the remediation lists the admitted kinds, including a vault overlay kind.
- [x] 1.4 Red first: `edit_note(action="supersede_observation")` supersedes a compact unit on an entity page, the target's bytes stay unchanged, and history records `why`.
- [x] 1.5 Red first: a span target inside a facet prose item converts only that item and supersedes it; an ambiguous span, a missing target and an already superseded target each refuse with their code and leave the page unchanged.
- [x] 1.5a Red first: `assess_observation` refutes a rich claim with an evidence link and keeps its text; `link_observation` adds a link and keeps existing relations; a compact target promotes only with `kind` (appended, same id, other units unchanged) and refuses without it, listing kinds; an unknown verdict refuses listing verdicts.
- [x] 1.6 Red first: `read_page(part="units")` shows `supersedes`, `superseded_by` and `as_of`; recall marks the target `superseded`; the `currency` dimension groups the same-page pair.
- [x] 1.7 Red first: the audit reports `entity_facet_unaddressed` for a facet prose item and not for link-only items, units or markdown relations; the dry-run plan writes nothing; the applied plan converts items in place.

## 2. Implementation

- [x] 2.1 Add the `replacement` kind trait to `KIND_TRAITS` and the `correction` kind (carrying it) to the core kinds pack; update the pack pins.
- [x] 2.2 Admit note-type role `entity` in `observe_memory` for unit writes without widening role `compiled`.
- [x] 2.3 Make both rich-kind refusals list the selected registry's kinds and name `kind` for a kind passed as `category`.
- [x] 2.4 Add the `supersede` operation to `observe_memory` and the `supersede_observation` action to `edit_note`; point the `supersede_note` description to it for one claim.
- [x] 2.4a Add the assess/link leaf operation to `observe_memory` and the `assess_observation` and `link_observation` actions to `edit_note`, sharing the supersede target resolver; forward `kind`, `as_of` and `verdict` on `edit_observation`.
- [x] 2.5 Add `as_of`, `supersedes` and derived `superseded_by` to `page_units`; prove the `currency` dimension groups same-page unit pairs.
- [x] 2.6 Add the `entity_facet_unaddressed` audit check and its `audit_fix` plan, sharing the in-place conversion with the span target.
- [x] 2.7 Exclude same-page `replaces` edges from `same_page_epistemic` in the relation census; make the `supersedes` teach line name units.
- [x] 2.8 Update the scaffold references (supersession, semantic authoring, operations, audit checks) generically; regenerate plugin trees, tool schemas and the tool-surface fingerprint, capability docs and the other derived artifacts.

## 3. Delivery

- [x] 3.1 Run the scoped tests, ruff, mypy, the OpenSpec strict gate and the privacy gate.
- [x] 3.2 One independent review of the finished batch on Opus with the Risk lens for stored data; merge through the queue.
- [x] 3.3 Release through the release owner and install on the desktop personal service. (Released in v0.202.0; the desktop personal service runs v0.204.0 since 2026-11-25.)
- [x] 3.4 Acceptance on the owner's installed service: redo the dogfood correction that motivated this change (see proposal, Why) with `supersede_observation` on the real entity page, confirm that a search for superseded claims on that page returns the old claim, and remove the interim correction prose. (2026-11-25 on v0.204.0: a span-targeted `supersede_observation` with `as_of` and an entity link committed on the entity page; `read_page(part="units")` shows both ends; a `search_memory` with `unit.dimension` = `currency` returns the old claim marked replaced; the interim prose is removed.)
- [x] 3.5 Sync the deltas into the canonical specs and archive this change.
