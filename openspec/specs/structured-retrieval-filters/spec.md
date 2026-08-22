# structured-retrieval-filters Specification

## Purpose
TBD - created by archiving change add-epistemic-loop-primitives. Update Purpose after archive.
## Requirements
### Requirement: Governed Unit Metadata Is Filterable
The structured-filter field registry SHALL accept `unit.verdict` as a closed string field and `unit.check_by` as a closed typed-date field. Both SHALL be evaluated against the same parsed semantic unit as the other `unit.*` fields, and a filter that names any other `unit.*` field SHALL still be rejected as unknown. `unit.verdict` operands SHALL be canonicalized by trimming and casefolding, and SHALL be rejected when they are not strings for the exact and collection operators.

#### Scenario: A verdict predicate compiles and matches
- **WHEN** a filter expression contains `{"unit.verdict": {"$eq": "Refuted"}}`
- **THEN** the plan compiles, carries a unit predicate, and matches a unit whose verdict is `refuted`

#### Scenario: A verdict predicate excludes other verdicts
- **WHEN** the same expression is evaluated against a unit whose verdict is `confirmed`
- **THEN** the unit does not match

#### Scenario: An absent verdict is distinguishable
- **WHEN** a filter expression contains `{"unit.verdict": {"$exists": false}}`
- **THEN** a unit carrying no verdict metadata row matches and a unit carrying one does not

#### Scenario: An unknown unit field stays rejected
- **WHEN** a filter expression names `unit.confidence`
- **THEN** compilation raises an unknown-filter-field error

### Requirement: Check By Answers Due-By Questions As A Typed Date
`unit.check_by` SHALL be typed as a date so ordered comparisons are available, SHALL reject a non-date operand, and SHALL reject `$contains`, matching the existing typed-date field contract. The evaluated runtime value SHALL be a real date rather than the raw authored string, so an ordered comparison against a date operand is decidable.

#### Scenario: A due-by query selects overdue predictions
- **WHEN** a filter expression contains `{"unit.check_by": {"$lte": "2026-11-01"}}`
- **THEN** a unit whose check-by date is `2026-10-01` matches and a unit whose check-by date is `2026-12-01` does not

#### Scenario: A non-date operand is refused
- **WHEN** a filter expression contains `{"unit.check_by": {"$eq": "soon"}}`
- **THEN** compilation raises an invalid-filter-value error

#### Scenario: Substring comparison is refused on the typed date field
- **WHEN** a filter expression contains `{"unit.check_by": {"$contains": "2026"}}`
- **THEN** compilation raises an invalid-filter-operator error
