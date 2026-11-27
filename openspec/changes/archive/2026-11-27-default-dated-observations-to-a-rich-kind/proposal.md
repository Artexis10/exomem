## Why

Two dogfood saves on 2026-11-25 failed on a rule that served no one.

1. `add_observation` with a `category`, a `text` and an `as_of` or `until` date, and no `kind`, was refused with `COMPACT_METADATA_REQUIRES_RICH_KIND`. A compact observation carries no metadata rows, and the refusal told the agent either to pass a rich `kind` or to omit the date. The agent dropped the date. The date was the reason it sent the observation, so the memory lost the one fact that mattered.
2. `add_observation` with `unlinked_reason` on an entity page was refused with `INVALID_RELATION_REVIEW`. An entity page takes no link review, so the reason has nothing to bind to. The refusal made the agent resend the same call without the field.

Both refusals guard against a state that cannot happen. The date can ride a rich unit, and a reason sent where no review applies can be dropped, as a creation already drops it.

## What Changes

- A new closed kind trait, `dated_default`, names the kind a dated observation takes when the caller names none. The shipped pack assigns it to `record`, the neutral kind. Only the shipped pack sets it: the registry ignores the trait on a vault kind and reports a warning finding, because the shipped kind is always admitted and a second carrier would refuse every dated save.
- `add_observation`, and `edit_note(action="edit_observation")`, with `as_of` and/or `until` and no `kind`, write a rich `record` unit with the dates. The choice reads the registry's trait, as `supersede_observation` reads `replacement`. No code compares a kind key. The reply says "Saved with its date." and does not name the kind, because "Record" already means a Records collection entry in a write's summary.
- A date sent with `verdict` or `check_by` and no `kind` keeps its refusal, but the refusal now offers two exits that keep the date: pass a kind that takes those fields, or omit `verdict` and `check_by` so the date lands in a dated block. No refusal offers to drop `as_of` or `until`. An explicit compact `kind` with dates refuses the same way: pass a rich kind, or leave `kind` out.
- Placement cost: a dated add is appended at the end of the page body as a rich block, not placed in the page's observation list. Dating a compact unit through an edit moves it the same way: the compact line leaves the observation list and the dated block, with the same anchor, goes at the end of the body. The unit id keeps its anchor and takes a new version suffix, as on any edit, so an id read before the edit no longer matches.
- The busy-write intake pre-refuses only a call that carries `verdict` or `check_by`, or a named compact `kind` with dates. A call with dates and no `kind` goes to the page-aware path.
- A write to a page that takes no link review (an entity page, or any page outside the active compiled set) accepts `unlinked_reason`, records no decision, and says in one sentence that it did not record it. A preview lists only the review fields the caller sent, so a typed tool's preview names `unlinked_reason`. A creation already drops the fields the same way. A page that needs a review keeps every existing check.
- The `as_of`, `until` and `unlinked_reason` field descriptions say when they apply: the `edit_note` `as_of` says that dating a compact unit moves it to a dated block, and `unlinked_reason` says that only active pages of a compiled type take a link review.

## Capabilities

### Modified Capabilities

- `semantic-write-contract`: the rich-kind refusal requirement gains the dated-default rule, a new requirement covers review fields sent for an existing page that takes no review, and the warning requirement for dropped review fields names the typed tool's field.
- `existing-edit-review-handshake`: a reviewed-none commit without a hash is refused only where a review applies.
- `semantic-unit-language`: compact observations stay metadata-free, and a shipped-only kind trait names the rich kind that holds a date.

## Impact

- Code: `src/exomem/observe_memory.py`, `src/exomem/semantic_kinds.py`, `src/exomem/semantic_language_registry.py`, `src/exomem/vocabulary/packs/core/kinds.yaml`, `src/exomem/semantic_writes.py`, `src/exomem/relation_review.py`, `src/exomem/semantic_contract.py`, `src/exomem/mutation_terminal.py`, `src/exomem/public_tools.py`, and the callers that pass review fields to an existing page (`edit.py`, `create_file.py`, `append_to_file.py`, `set_frontmatter_field.py`).
- Public surface: four field descriptions change (`as_of` and `until` on `add_observation`, `as_of` on `edit_note`, and the shared `unlinked_reason`). The MCP discovery fingerprint changes, so hosted connectors need a refresh after release.
- Data: a dated observation now lands as a `## Record` block with its date rows at the end of the page body. No stored page changes.
- Old debt, not fixed here: `observe_memory._COMPACT_KIND = "observation"` compares an entry key, and the `replacement` trait keeps the two-carrier trap that this change closes for `dated_default` (see `design.md`).
