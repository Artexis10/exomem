## ADDED Requirements

### Requirement: As Of And Until Are Governed Date Unit-Metadata Keys
The semantic-unit language SHALL recognize `as_of` and `until` as governed rich unit-metadata keys, parsed as strictly as `check_by`: each value SHALL be a strict ISO calendar date spelled `YYYY-MM-DD`. `as_of` states when the unit became true, and `until` states the last date on which it holds. A value that is not an exact ISO calendar date SHALL produce a deterministic source-addressed error, `invalid_rich_as_of` or `invalid_rich_until`. An `until` earlier than the same unit's `as_of` SHALL produce the error `invalid_rich_date_order`. Valid values SHALL be projected onto the parsed unit and included in its serialized form. Neither key SHALL change a unit's lifecycle; currency reads them at serve time (context-activation).

#### Scenario: Valid dates are projected
- **WHEN** a rich unit carries `- as_of: 2026-05-01` and `- until: 2026-12-31`
- **THEN** the parsed unit's `as_of` is `2026-05-01`, its `until` is `2026-12-31`, and the document reports no error

#### Scenario: A timestamp is rejected
- **WHEN** a rich unit carries `- until: 2026-12-31T23:59:00Z`
- **THEN** the document reports an `invalid_rich_until` error bound to that source line

#### Scenario: Reversed dates are rejected
- **WHEN** a rich unit carries `- as_of: 2026-05-01` and `- until: 2026-04-01`
- **THEN** the document reports an `invalid_rich_date_order` error

## MODIFIED Requirements

### Requirement: Governed Unit Metadata Is Rich-Form Only And Reserved
Governed unit-metadata keys SHALL be available only in the rich authoring form, because compact observations carry no metadata rows. `verdict`, `check_by`, `as_of` and `until` SHALL be treated as reserved metadata rows, so a rich unit whose only content is governed metadata SHALL still be reported as an empty rich unit.

#### Scenario: A metadata-only prediction is still empty
- **WHEN** a page contains `## Prediction` followed only by `- verdict: refuted` and `- check_by: 2026-11-01`
- **THEN** the document reports an `empty_rich_unit` error for that heading

#### Scenario: Compact observations carry no governed metadata
- **WHEN** a compact observation line is parsed
- **THEN** the resulting unit has no verdict and no check-by value
