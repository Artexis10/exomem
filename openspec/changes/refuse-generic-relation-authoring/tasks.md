# Tasks

## 1. Barrier and menu

- [x] 1.1 Refuse every generic relation a write adds, for every pair, with `GENERIC_RELATION_REFUSED`. Remove `relation_disposition="generic"`, the `create-entity` relation `reason`, and the per-edge reason bookkeeping. Check: a create and an edit that add a note-to-note generic edge are refused; an entity edge is refused; an edit that leaves a legacy generic edge commits; a draft and `adoption_compile` pass; a successor's carried edge passes.
- [x] 1.2 Build the ranked menu in `relation_options.py` and use it for the refusal and the generators. Check: ranking keys apply in order; at most eight options; a withheld target gives the menu that an absent one does.
- [x] 1.3 Spell the three routes as exact calls. Check: the retry with the top key commits; the plain-link bullet commits; the `propose-relation` and `save-relations` calls match the served surface.

## 2. The product's own writers

- [x] 2.1 Render bare `connections` as plain links in `link.py`. Check: `create-entity` with `connections` writes plain-link bullets.
- [x] 2.2 Make similarity and co-participation candidates name no relation, carry the options and the plain link, and require a choice to accept. Check: accepting a shared-source candidate is refused without a choice and commits with one; the plain link commits; the generic relation is refused.
- [ ] 2.3 Stop Studio sending the generic relation. Check: the relation review view accepts with a chosen relation or the plain link.
- [x] 2.4 Delete `relation_offer.py`, its post-commit projection and the tests that exercised only it.
- [x] 2.5 List the disposition of every caller of `relation_registry.generic_relation_key()` and every `relates_to` literal under `src/exomem`. Check: the PR lists each call site as kept for legacy reads, changed, or reported debt.

## 3. Census

- [x] 3.1 Add `specific_share` and `plain_link_share` to the new-edge cohort. Check: both count only authored relation edges; both report unavailable when the log names no relation.

## 4. Teaching

- [x] 4.1 Teach "name a specific relation; if none fits, propose a new type; a plain link only as a last resort" in the scaffold skill and the sample vault skill, and never suggest the generic relation. Regenerate the skill-contract stamps, the packaged skills and the cloud plugin through their generators. Check: the teaching text appears in each generated copy; the scaffold leak test passes.

## 5. Specification

- [x] 5.1 Move the barrier, options, generators and census requirements out of `promote-referents-without-nudges` into this change.
  Its tasks 2.7, 2.8 and 2.9 and the census requirement point here, and its stale semantic-write-contract requirement (already in the canonical spec) is removed so the rule lives in one place.
- [ ] 5.2 Edit the canonical `Purpose` line of `graph-semantic-integrity` at archive: similarity-only and co-participation suggestions carry no relation, rather than `relates_to`.
- [x] 5.3 Run `openspec validate --all --strict` with the pinned CLI before and after the work.
