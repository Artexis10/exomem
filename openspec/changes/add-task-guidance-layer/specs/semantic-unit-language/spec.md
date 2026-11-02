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

### Requirement: Guidance Declarations Are Governed Unit-Metadata Keys
The semantic-unit language SHALL recognize three governed rich unit-metadata keys for task guidance (task-guidance):

- `activities`: a comma-separated list of at most 16 activity keys, written as `tags` rows are, each matching the activity-key grammar;
- `guidance`: one guidance-role key;
- `guidance_origin`: exactly `inferred` or `reviewed`.

A malformed key, more than 16 activity keys, or any other `guidance_origin` value SHALL produce a deterministic source-addressed error: `invalid_rich_activities`, `invalid_rich_guidance` or `invalid_rich_guidance_origin`. A well-formed key that the vault's registry does not hold SHALL produce the source-addressed finding `unregistered_activity` or `unregistered_guidance_role`, SHALL NOT fail the parse, and SHALL match no declaration. Valid values SHALL be projected onto the parsed unit and included in its serialized form.

#### Scenario: A declared precedent is projected
- **WHEN** both activity keys are registered and a rich unit carries `- activities: reply-message, review-contract` and `- guidance: precedent`
- **THEN** the parsed unit's activities are `reply-message` and `review-contract`, its guidance role is `precedent`, and the document reports no error

#### Scenario: An unregistered activity is a finding, not an error
- **WHEN** a rich unit carries `- activities: plan-trip` and the registry has no `plan-trip`
- **THEN** the document reports an `unregistered_activity` finding bound to that line, parses the unit, and the unit matches no declaration

#### Scenario: An unknown origin is rejected
- **WHEN** a rich unit carries `- guidance_origin: approved`
- **THEN** the document reports an `invalid_rich_guidance_origin` error

## MODIFIED Requirements

### Requirement: Governed Unit Metadata Is Rich-Form Only And Reserved
Governed unit-metadata keys SHALL be available only in the rich authoring form, because compact observations carry no metadata rows. `verdict`, `check_by`, `as_of`, `until`, `activities`, `guidance` and `guidance_origin` SHALL be treated as reserved metadata rows, so a rich unit whose only content is governed metadata SHALL still be reported as an empty rich unit.

#### Scenario: A metadata-only prediction is still empty
- **WHEN** a page contains `## Prediction` followed only by `- verdict: refuted` and `- check_by: 2026-11-01`
- **THEN** the document reports an `empty_rich_unit` error for that heading

#### Scenario: Compact observations carry no governed metadata
- **WHEN** a compact observation line is parsed
- **THEN** the resulting unit has no verdict and no check-by value

#### Scenario: A metadata-only guidance unit is still empty
- **WHEN** a page contains a rich heading followed only by `- activities: review-contract` and `- guidance: method`
- **THEN** the document reports an `empty_rich_unit` error for that heading
