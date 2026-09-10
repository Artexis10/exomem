## MODIFIED Requirements

### Requirement: Separate capacity alerts and hard consumption cutoff

The preflight SHALL distinguish an endpoint capacity ceiling, an alert-only monthly spending threshold and an optional provider-enforced monthly compute quota. A requested compute quota SHALL be verified in weighted CU-seconds; zero or missing means unlimited. The preflight SHALL not enable or modify a quota. Operator guidance SHALL state that quota exhaustion stops the shared control plane, storage and transfer remain billable, provider usage can lag and alert configuration is not proof of email delivery. Operator guidance SHALL additionally state the always-on floor for the configured minimum endpoint size, and SHALL state that a threshold below that floor cannot restrain spending and only schedules an outage.

#### Scenario: No hard cutoff selected

- **WHEN** the operator has not requested monthly compute-quota verification
- **THEN** the preflight reports the observed quota without treating an alert as a hard stop

#### Scenario: Explicit 100-CU-hour quota policy

- **WHEN** the operator requests a 100-CU-hour quota check
- **THEN** a positive provider compute quota no greater than 360000 CU-seconds passes and an unlimited or larger quota fails

#### Scenario: Requested ceiling sits below the always-on floor

- **WHEN** a requested quota or spending threshold is lower than the cost of holding the configured minimum endpoint open for the whole billing period
- **THEN** the guidance names it as an outage schedule rather than a budget, and the preflight reports the floor alongside the requested ceiling

#### Scenario: Ongoing background work

- **WHEN** existing maintenance keeps the database active at its minimum
- **THEN** the runbook identifies the retained compute baseline and does not claim the capacity ceiling makes idle usage free

## ADDED Requirements

### Requirement: Placement is a budget control

The preflight SHALL classify every declared consumer of a metered scale-to-zero endpoint as request-driven or always-on, where always-on means the consumer contacts the database on a fixed interval shorter than the endpoint's autosuspend window. An always-on consumer sharing a metered scale-to-zero endpoint SHALL be a named failed control, and the guidance SHALL identify relocation of that consumer, not a plan change, threshold or quota, as the remedy. Consumers SHALL be resolved by endpoint identity rather than by hostname, because a pooled and a direct hostname can address one endpoint.

#### Scenario: A poller shares the metered endpoint

- **WHEN** a worker polling on a fixed one-second interval shares an endpoint whose autosuspend window is five minutes
- **THEN** the preflight fails the placement control, names that consumer, and reports the resulting duty cycle rather than reporting an unexplained baseline

#### Scenario: Two hostnames, one endpoint

- **WHEN** two services connect through a pooled hostname and a direct hostname that resolve to the same endpoint id
- **THEN** the preflight counts them as consumers of one endpoint rather than as two independent databases

#### Scenario: Only request-driven consumers remain

- **WHEN** every remaining consumer contacts the database only on request or on an interval longer than the autosuspend window
- **THEN** the placement control passes and the observed duty cycle is reported as evidence
