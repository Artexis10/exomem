# Tasks

## 1. Baseline

- [ ] 1.1 Run the write probe on the base revision: 460,800 rows, two repeats per key shape, with the run files kept for the PR.

## 2. Ordered item keys

- [ ] 2.1 Add store revision 14: `collections.item_key_scheme` with its two triggers; an upgrade sets `hashed-v1` and rebuilds nothing.
- [ ] 2.2 Add the `ordered` identity codec and the `ordered-v1` encoding, with frozen vectors for both schemes.
- [ ] 2.3 Set the scheme at creation, take the layout codec from the scheme, and use one scheme-aware key function in collapse, derived items, preview and rebuild.
- [ ] 2.4 Refuse a location classification of a natural-key field of an `ordered-v1` collection, and give a natural key with a location field `hashed-v1`.
- [ ] 2.5 Compile `key()` comparisons for the `ordered` codec, and add the key length check to the derived table.
- [ ] 2.6 Report the scheme and key byte lengths in inspection, `describe` and preview, and state the default row order in `describe`.

## 3. Open-member revert

- [ ] 3.1 Record `row_floor` and `restored` when a member opens, and save each earlier row the member updates into the undo table.
- [ ] 3.2 Assess, settle and step the revert in bounded steps, with `reverting` progress, and fall back to a full rebuild under the listed conditions.
- [ ] 3.3 Test a revert mid-member that updated earlier rows, including a restart in the middle of the revert.

## 4. Derived connection

- [ ] 4.1 Set `journal_size_limit` to 64 MiB on the derived connection.
- [ ] 4.2 Run the cache and checkpoint ablation, and keep a setting only if it meets the bar in the design.

## 5. Time bases and row paths

- [ ] 5.1 Add `floating`, minute precision, `milliseconds` and `duration` increments, and `local_datetime`, with their compile refusals.
- [ ] 5.2 Add object and root row paths, and refuse `$index` when the row path crosses no array.
- [ ] 5.3 Count preview rows by resolution kind, and state in `describe` that instant filters exclude floating rows.
- [ ] 5.4 Prove that an existing mapping keeps its canonical hash.
- [ ] 5.5 Import one fixture that uses every new form, checked against hand-computed answers.

## 6. Proof and delivery

- [ ] 6.1 Commit the write probe under `benchmarks/`, run it on the change for the three key shapes and the cancel arm, and cite the run files in the PR.
- [ ] 6.2 Review the integrated change independently, with stored data as the main risk; resolve findings.
- [ ] 6.3 Merge, then synchronize this delta into the canonical specs and archive this change with `openspec archive`.
