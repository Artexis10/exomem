## 0. Owner decision

- [ ] 0.1 The owner approves this design, including reverse expansion (D7), before implementation starts.

## 1. Declared references (phase 1)

- [ ] 1.1 Parse and validate `references` on string fields, arrays of strings and object properties in arrays, with revise refusals that name what to change.
- [ ] 1.2 Add the reference index to the collection store, maintained in the canonical item transaction and rebuilt like other projections; backfill existing rows.
- [ ] 1.3 Report resolution counts and distinct unresolved values in `inspect`, and warn on writes that store an unresolved value.

## 2. Related reads (phase 1)

- [ ] 2.1 Resolve references at read time with per-target admission and field plans; withheld and missing twins read the same.
- [ ] 2.2 Add `related` expansion and filters through a single-valued reference to `read_records` and `read_plans` queries, within the result and visit budgets.
- [ ] 2.3 Add `combined` (count, row refs, `unresolved_rows`) to aggregates on the same snapshot.
- [ ] 2.4 Amend `add-collection-query-engine` CQ4 and tasks 3.5–3.8 so that joins use these declarations.

## 3. Reverse expansion (phase 2)

- [ ] 3.1 Add bounded inverse expansion with exact counts, consulting only admitted collections' declarations.
- [ ] 3.2 Document how an owner retires a hand-kept back-link field once inverse expansion covers it.

## 4. Acceptance

- [ ] 4.1 End-to-end test on invented fixtures: a diary, sites and skills; one call that filters through `site`, expands both references and totals hours and leave days, with missing, renamed, withheld and ambiguous targets.
- [ ] 4.2 Ask the motivating question in plain language on a real model against a disposable copy of a real vault. Add the hostile cases: a missing site, a renamed site, conflicting hours and a collection the caller cannot read. The agent must answer in one query, name the rows behind each total, and show unresolved rows.
- [ ] 4.3 Measure expansion on the largest real collections against the interactive budget; record whether materialisation is needed.
