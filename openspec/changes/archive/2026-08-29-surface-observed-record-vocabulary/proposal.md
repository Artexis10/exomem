## Why

A Records manifest that declares `event_type: {type: string, required: true}` tells an
appending agent the field exists and is mandatory, but nothing about the vocabulary the
collection already uses. A field declared as `enum` carries its own vocabulary and
`describe` already returns it, so the gap is confined to free-string fields: today an
agent appending to a live collection can only echo the user's phrasing, and the
collection's own terms drift one append at a time.

Inspection already parses every authorized item to report snapshot, audit and
representation debt. The values are in hand; they are simply discarded.

## What Changes

- Report, on `record_memory(action="inspect")` of a collection, an additive
  `observed_values` summary for every declared string-typed field without a declared
  `enum`: the distinct values in use with their occurrence counts.
- Bound the summary: at most 20 distinct values per field, each value cut to 120 characters
  for display, with a per-field `truncated` flag that says so when the cap binds and a
  per-value `value_truncated` flag that says so when the display cut applies.
- Count values by their full text with surrounding whitespace removed, and rank by
  frequency before the cap is applied, so the cap drops a collection's rarest terms rather
  than the ones the item pass happened to meet last. Counting on the shortened form would
  merge two distinct terms sharing a long prefix into one entry with a summed count.
- Exclude `enum`-declared fields. The declaration is the vocabulary; restating observed
  usage there would invite an agent to treat a gap in usage as a gap in the contract.
- Compute the summary inside the item pass inspection already performs, so it inherits
  the same serve-time disclosure filtering as the rest of the item-derived payload. A
  value carried only by items this audience may not read does not appear.
- Omit the key entirely when no item pass ran, so an unreadable collection never reports
  an empty vocabulary as though it had been swept.
- Limit v1 to fields declared `type: string`. Array-of-string fields carry a vocabulary too
  and are a reasonable follow-up, but their summary needs its own answer for how an item
  contributing several values should count, and that decision does not belong in a change
  whose purpose is to close the scalar gap.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `records`: Collection inspection reports the observed vocabulary of free-string fields
  under the disclosure filtering that already governs item-derived inspection data.

## Impact

`record_formats.inspect_collection`, the `CollectionInspection` result, the
`record_inspection` egress projector and its payload validator, and their focused tests
change. No mutation surface, tool schema, tool description, manifest contract, query
result, or `describe` response changes. The addition is read-only and additive: every
existing inspection key keeps its shape.
