## 1. Red-first contract tests

- [x] 1.1 Add a red-first test proving inspection reports a free-string field's distinct
  values with counts.
- [x] 1.2 Add a red-first test proving the distinct-value cap binds at 20 and sets the
  per-field truncation flag.
- [x] 1.3 Add a red-first test proving an `enum`-declared field is not summarized.
- [x] 1.4 Add a red-first disclosure test proving a value carried only by a withheld item
  never reaches the payload.
- [x] 1.5 Add a red-first test proving two distinct values sharing the display window stay
  two entries with their own counts, each flagged as display-truncated.
- [x] 1.6 Add a red-first test proving the cap retains the most frequent values rather than
  the first ones seen.
- [x] 1.7 Add a red-first test proving a withheld item does not raise the count of a value
  it shares with released items.
- [x] 1.8 Pin that an unreadable collection omits the key, and that the legacy-tracker
  inspection union refuses a payload carrying it.

## 2. Bounded observed-value summary

- [x] 2.1 Compute the summary inside the item pass `record_formats.inspect_collection`
  already performs, with no additional read.
- [x] 2.2 Count on the full stripped value, rank by descending count with an ascending-value
  tie-break, then cap; flag per-field truncation and per-value display cuts.
- [x] 2.3 Report `None` rather than an empty summary when no item pass ran.

## 3. Governed egress

- [x] 3.1 Emit the summary from `record_governance.inspect_collection` only when the item
  pass produced one.
- [x] 3.2 Extend the `record_inspection` projector allowlist and payload validator to
  re-validate the summary's bounds and its exact entry key set on the way out, without
  asserting display-form distinctness a legitimate collision would break.
- [x] 3.3 Keep the legacy-tracker inspection union free of the new key.

## 4. Verification

- [x] 4.1 Run the Records mutation, formats, governance, lifecycle, presentation and
  structured-file-migration suites.
- [x] 4.2 Run `ruff check src/exomem --select F`.
- [x] 4.3 Mutation-prove the summary computation, the disclosure filter, the enum
  exclusion, the cap, the frequency selection, the full-value counter key and the egress
  allowlist in an external scratch copy.
- [x] 4.4 Validate the OpenSpec change in strict mode.
