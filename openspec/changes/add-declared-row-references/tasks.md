## 0. Owner decision

- [ ] 0.1 The owner approves this design, including inverse expansion (D7), before implementation starts.

## 1. Declared references and forward reads (phase 1)

- [ ] 1.1 Parse and validate `references` on string fields, arrays of strings and object properties in arrays; generic refusal for missing, foreign or withheld targets first, then key and field-type refusals that name what to change.
- [ ] 1.2 Resolve forward references at read time through the target's natural-key index or canonical refs, with per-target admission, field plans and link projectors; withheld and missing twins read the same.
- [ ] 1.3 Add `related` expansion and filters through a single-valued reference to `read_records` and `read_plans` queries, within the result and visit budgets; cursors bind every target's basis.
- [ ] 1.4 Add filtered base-row reductions returning rows and `combined` (`count`, `unresolved_rows`, `excluded_unresolved`) from one filter and snapshot.
- [ ] 1.5 Warn on writes under the writer's own view, with bounded import warnings; report resolution counts and distinct unresolved values in `inspect`.
- [ ] 1.6 Report saved views that use a removed declaration as stale; keep unrelated revises working when a target disappears.

## 2. Inverse expansion (phase 2)

- [ ] 2.1 Add `item_references` by a new collection-store schema revision, maintained in the canonical item transaction, kept out of the in-vault replica and rebuilt from items; reads report warming until it is built.
- [ ] 2.2 Add bounded inverse expansion with exact counts, consulting only admitted collections' declarations, and list inbound declarations in `inspect`.
- [ ] 2.3 Document how an owner retires a hand-kept back-link field once inverse expansion covers it.

## 3. Acceptance

- [ ] 3.1 End-to-end test on invented fixtures: a diary, sites and skills; one call that filters through `site`, expands both references and totals hours and leave days, with missing, renamed, withheld and field-withheld targets.
- [ ] 3.2 Ask the motivating question in plain language on a real model against a disposable copy of a real vault, with the hostile cases: a missing site, a renamed site, conflicting hours and a collection the caller cannot read. The agent must answer in one query, name the rows behind each total and show unresolved and excluded rows.
- [ ] 3.3 Measure expansion on the largest real collections against the interactive budget; record whether materialisation is needed.
