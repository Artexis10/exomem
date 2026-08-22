## ADDED Requirements

### Requirement: The Prediction Kind Satisfies The Shared Write Contract
The shared semantic write contract SHALL recognize a valid `prediction` rich unit exactly as it recognizes every other governed kind. A compiled active page whose only semantic unit is a substantive `## Prediction` block SHALL satisfy the minimum-semantic-unit rule, and a governed writer SHALL accept `prediction` as an explicit kind without a registry entry.

#### Scenario: A prediction alone satisfies the minimum unit rule
- **WHEN** an active compiled page is written whose only semantic unit is a substantive `## Prediction` rich block
- **THEN** the precommit contract reports no `missing_semantic_unit` finding

#### Scenario: A governed writer accepts the prediction kind
- **WHEN** a caller adds a semantic unit through the single-unit mutation route with kind `prediction`
- **THEN** the unit is written as a rich `## Prediction` block rather than refused as an unsupported kind

### Requirement: Loop Primitives Are Exempt From Quota Logic
Neither the `prediction` kind nor the `verdict` and `check_by` metadata keys SHALL introduce, satisfy, or alter any count-based obligation beyond the existing one-unit minimum. No contract SHALL require a page to contain a prediction, require a prediction to carry a verdict, or treat a verdict as changing how many units a page needs.

#### Scenario: A page without a prediction is not penalized
- **WHEN** an active compiled page satisfies the minimum-unit rule with a non-prediction unit
- **THEN** the precommit contract reports no finding that references predictions

#### Scenario: A prediction without a verdict is valid
- **WHEN** an active compiled page contains a substantive `## Prediction` rich block carrying no `verdict` metadata row
- **THEN** the precommit contract reports no error for the missing verdict

#### Scenario: Adding a verdict does not change the unit obligation
- **WHEN** the same page is written once without and once with a `- verdict: refuted` row on its only unit
- **THEN** both writes report the same minimum-semantic-unit outcome
