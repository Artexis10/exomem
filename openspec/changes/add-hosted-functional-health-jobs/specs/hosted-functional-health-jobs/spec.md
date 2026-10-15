# Hosted functional health jobs

## ADDED Requirements

### Requirement: Functional jobs remain optional and isolated
The platform SHALL provide optional control, email and memory observations. New jobs SHALL be disabled by default.
Disabled jobs SHALL be absent from scheduled workloads and observability inputs. Existing schedules and pod probes SHALL remain unchanged.

#### Scenario: The default chart is rendered
- **WHEN** no functional health feature is enabled
- **THEN** the new jobs and functional route are absent, without new missing-run alerts

#### Scenario: HTTP health jobs are enabled
- **WHEN** the deployment enables the control and email jobs
- **THEN** they use the existing authenticated routes at one-minute and thirty-minute cadences respectively

### Requirement: Memory observations use one synthetic principal
The memory runner SHALL use one configured mutable page, dedicated bearer and restricted proof function.
Its network authority SHALL include only the selected synthetic cell, DNS and configured database addresses.
It SHALL receive no gateway identity or customer-fleet access.

#### Scenario: The memory runner is enabled
- **WHEN** the synthetic namespace, page, credentials and roles exist
- **THEN** the runner records its bounded observation through the restricted function without customer access

### Requirement: Public health routing preserves liveness
The optional public route SHALL accept only GET `/healthz` and rewrite it to the functional memory reader.
Direct pod liveness and readiness SHALL remain unchanged.

#### Scenario: A public health request arrives
- **WHEN** the functional route is enabled and receives GET `/healthz`
- **THEN** the existing gateway returns the persisted functional result through its trusted ingress

### Requirement: Schedule data has one source
The canonical schedule contract SHALL supply cadences and job identities to chart workloads and observability.
The packaged chart SHALL contain the same contract bytes.

#### Scenario: The chart is packaged
- **WHEN** Helm packages the repository chart
- **THEN** it materializes the canonical contract and preserves directory-render behavior
