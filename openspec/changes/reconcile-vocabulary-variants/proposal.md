## Why

Tags drift into variants of one word: `dogfood` beside `dogfooding`, `failure` beside
`failures`, `x_y` beside `x-y`. A measured vault held 5,010 distinct tags, of which 119
groups were inflection or separator variants of one word, across 1,185 tag uses. Nothing
reconciled them, so retrieval, collection claims and routing split one concept in two.

Writers already lowercase tags and map `_` and spaces to `-`, but only for newly authored
tags, and never across inflections. The audit's `tag_inconsistency` category groups case and
separator variants but stays propose-only, with no governed way to act on it.

Separately, live writes reported `vocabulary_sync: {state: unavailable, reason:
guidance_unavailable}` with no way to see why. The public projection collapsed every
unavailable projection into that one reason, and the catch-all in post-commit delivery
swallowed exceptions without a log line. This change makes the cause observable; it does not
yet identify the live cause.

## What Changes

- One shared, conservative term fold, `vocabulary_fold.fold_term`, for tags, collection
  claims and routing. It applies NFKC, casefolds, maps runs of `_`, whitespace and `-` to one
  `-`, and removes one plural (`-s`/`-es`, `-ies` to `-y`) from the final segment. It never
  removes `-ing` or `-ed`. Words under four letters and an `EXCEPTIONS` set are never
  inflected.
- Write-time tag advice. Authored tags are never rewritten at any prominence level. When an
  authored tag is a fold-variant of a more-used tag, a write at `maximal` keeps it and adds a
  warning naming the canonical tag; at any non-`off` level the committed response carries one
  one-line `vocabulary_advisory` in the existing advisory slot.
- `maintain_memory(mode="tag-variants")`: a read-only preview lists variant groups with the
  page counts the caller may see and one bounded batch; `apply=true` with the exact preview
  `plan_id` and a one-line `why` rewrites that batch's minority variants to the canonical tag
  and logs a rollback record. Only the `tags` key changes. Pages in Sources, Evidence, Records,
  Planning, `_Adoption`, workflow contracts, schema and infrastructure trees are never counted
  or rewritten, and a tie is never rewritten.
- `vocabulary_sync` names the closed projection reason (for example
  `graph_projection_unavailable`) instead of collapsing it, and swallowed guidance and
  recovery failures are logged at warning level by exception type and raising location,
  never by content.

## Impact

- Code: `vocabulary_fold.py` (new), `tag_variants.py` (new), `vocabulary_delivery.py`,
  `note.py`, `edit.py`, `add.py`, `link.py`, `lexstore.py` (per-page catalogue tags),
  `curation.py` (shared owned-tree set), `commands.py`, `governance/egress.py`,
  `writer_lease.py`; `collection_claims` routing now meets plurals but not `-ing`/`-ed` forms.
- Surface: one new `maintain_memory` mode; the advisory slot can carry a tag notice.
- No migration. Existing tags change only through an explicitly confirmed maintenance batch.
