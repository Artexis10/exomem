## Context

Compact observations (`- [category] text`) hold no metadata rows by design, so a date on one has nowhere to live. The earlier refusal pushed the choice onto the agent, which chose to lose the date. `supersede_observation` already solved the same shape for its replacement unit: it selects the one admitted kind that carries the `replacement` trait.

Sources consulted: Anthropic, "Writing effective tools for agents" (2025): a tool should absorb a routine decision instead of refusing and teaching the caller a workaround.

## Decisions

### D1. A closed kind trait names the carrier, not a kind key

The carrier is the kind with trait `dated_default`: `language.kinds_with("dated_default")`. The trait is a promise that code keeps, so it joins `KIND_TRAITS`. Code never compares a kind key (registry rule C4).

### D2. Only the shipped pack sets the trait

The shipped kinds are always admitted. A vault kind that also carried the trait would leave two carriers, and every dated save would refuse with no kind to default to. So the registry ignores `dated_default` on a vault kind and reports it as a warning finding (`trait_ignored`, path `kinds.<key>.traits`) in the same findings list as every other vault-schema diagnostic (`core_category_shadowed` is the precedent). The vault still loads, and every page has exactly one carrier, so no refusal path for zero or several carriers exists.

### D3. The carrier is `record`

`record` is the neutral kind. `finding` carries the `assertion` trait, so a dated note written as a finding would become an assertion block that accepts `inference_mode`, a claim the caller never made. A dated fact is a record of something that held between two dates, nothing more.

### D4. Only dates default, and no refusal drops them

`verdict` and `check_by` name a prediction or review shape that a default kind cannot guess, so they keep the refusal. An explicit `kind="observation"` with dates is a stated contradiction and also refuses. A refusal never offers to drop `as_of` or `until`: the caller sent them to keep them. When dates arrive with `verdict` or `check_by` or a named compact `kind`, the two exits are to pass a rich kind that takes the fields, or to leave the other fields (and the compact `kind`) out so the date lands in a dated block. A call that names a rich unit's compact `kind` with a date is not offered the conversion to compact, which clears dates.

### D5. The decision comes before the relation handling

Links on the call are handled as for a rich unit, because the call is one. Governed values normalise before the decision, so a bad date still names the date.

### D6. An edit moves the unit to the end of the page

A compact unit given a date and no `kind` follows the existing promotion path of `edit_observation`: the rich block is appended at the end of the page body and the compact line is removed. The anchor stays, and the unit id takes a new version suffix, as on any edit. The assess promotion cannot lose a date, because `assess_observation` refuses `as_of` and `until`.

The placement is a cost. A dated block does not sit in the page's observation list beside its neighbours, and a reader sees it after every other section. This change accepts it because a rich unit is appended to the page body today, for an add as for an edit. Deferred follow-up, not built here: place a dated block inside the page's observation list.

### D7. Review fields on a page that takes no review are dropped with one warning

A creation already drops `relation_disposition`, `relation_review_hash` and `relation_review_reason` for a page that needs no review, and returns one warning. The existing-page seam refused instead. It now follows the creation rule: it records no decision, reports the fields under `relation_review_fields.ignored` in a preview, and adds the same warning to a commit. A typed tool sends the whole review as `unlinked_reason`, so its preview and its warning name that field, not the grouped fields the tool fills in. The shape test is the one that already words `RELATION_DISPOSITION_MISSING`, now shared.

### D8. The reply names the date, not the kind

The reply says "Saved with its date." and never names the kind. In a write's summary "Record" already means a Records collection entry, as in "; not filed in <collection>". A reply that said "as a Record" could sit beside that phrase and report two things that contradict each other. The tool descriptions follow: they say a dated observation goes into a dated block.

## Risks

- A page that gains a qualifying relation later no longer needs the review it once did. The writer judges applicability per write, as before.
- An agent that holds a compact unit's id across a dating edit must read the new id from the reply. The reply returns it.

## Old debt, not changed

- `replacement` has the same shape as `dated_default`. `supersede_observation` reads `kinds_with("replacement") & admitted`, so a vault kind that declares the trait leaves two carriers, and a supersede with no `kind` refuses with `REPLACEMENT_KIND_REQUIRED` until the caller names the kind. The refusal names the kinds and the caller can recover, so it is milder than the dated case, but the registry does not ignore the trait on a vault kind. A vault that adds a successor kind may rely on it, so this change leaves it.
- `observe_memory._COMPACT_KIND = "observation"` compares an entry key.
