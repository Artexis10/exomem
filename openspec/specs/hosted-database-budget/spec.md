# hosted-database-budget Specification

## Purpose
Make hosted database spending controls verifiable without waking database compute, exposing credentials or silently trading service availability for a budget target.

## Requirements

### Requirement: Read-only budget verification

The hosted budget preflight SHALL read provider management metadata only, SHALL perform no SQL or provider mutation, and SHALL verify endpoint capacity, branch inventory and an enabled spending-alert threshold against explicit policy. Default policy SHALL allow one endpoint, one production branch, a 0.25-CU minimum, a maximum no greater than 1 CU, enabled autosuspend and a spending threshold no greater than 500 cents. It SHALL never automatically delete branches.

#### Scenario: Current constrained project

- **WHEN** the project has one production branch and endpoint, 0.25–1 CU, provider-default autosuspend and a 500-cent alert
- **THEN** the preflight reports those controls as passing without waking PostgreSQL

#### Scenario: Capacity or inventory drift

- **WHEN** an endpoint can scale beyond policy or an additional branch or endpoint appears
- **THEN** the preflight reports a failed control and exits nonzero without altering any resource

### Requirement: Honest incomplete evidence and secret-free output

The preflight SHALL report an unreadable, missing, malformed or unauthenticated provider response as unknown and non-passing. It SHALL retain independent findings from other readable endpoints. Its report SHALL contain only allowlisted control observations, not credential values, raw provider errors, database URLs or arbitrary response fields.

#### Scenario: Expired credentials

- **WHEN** provider authentication fails
- **THEN** the preflight exits nonzero with a named unavailable check and no raw credential or provider error output

#### Scenario: Partial inventory failure

- **WHEN** branch inventory cannot be read but project, endpoint and spending data can
- **THEN** those available controls remain evaluated and the overall result is non-passing because branch inventory is unknown

### Requirement: Separate capacity alerts and hard consumption cutoff

The preflight SHALL distinguish an endpoint capacity ceiling, an alert-only monthly spending threshold and an optional provider-enforced monthly compute quota. A requested compute quota SHALL be verified in weighted CU-seconds; zero or missing means unlimited. The preflight SHALL not enable or modify a quota. Operator guidance SHALL state that quota exhaustion stops the shared control plane, storage and transfer remain billable, provider usage can lag and alert configuration is not proof of email delivery.

#### Scenario: No hard cutoff selected

- **WHEN** the operator has not requested monthly compute-quota verification
- **THEN** the preflight reports the observed quota without treating an alert as a hard stop

#### Scenario: Explicit 100-CU-hour quota policy

- **WHEN** the operator requests a 100-CU-hour quota check
- **THEN** a positive provider compute quota no greater than 360000 CU-seconds passes and an unlimited or larger quota fails

#### Scenario: Ongoing background work

- **WHEN** existing maintenance keeps the database active at its minimum
- **THEN** the runbook identifies the retained compute baseline and does not claim the capacity ceiling makes idle usage free
