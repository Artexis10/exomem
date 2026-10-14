# Cloud platform operations

## MODIFIED Requirements

### Requirement: External schedules remain exact and observable

The platform SHALL consume the checked versioned schedule contract at `infra/contracts/exomem-hosted-schedules-v1.json`.
The contract SHALL define the kept access-delivery and reconciliation routes, plus optional control and email observation routes.
Each job SHALL declare its cadence. New functional jobs SHALL remain disabled by default.
Renderers SHALL use the contract's origin, method, path, redirect refusal, timeouts, deadlines, retries, history and metric settings exactly.
Disabled jobs SHALL be absent from workloads and observability inputs.
The dedicated scheduler bearer SHALL authorize only the declared routes, with the contract's receiver overlap during rotation.
Global cron, database, payment, cell and provider credentials SHALL NOT enter scheduler Jobs.
The scheduler SHALL suspend when both Cloud control workloads are disabled.
The removed export garbage collector SHALL NOT be rendered or required by Cloud.

#### Scenario: A schedule renders
- **WHEN** the platform selects the supported schedule contract
- **THEN** each enabled Job matches its declared request, execution and content-free observability settings

#### Scenario: A response redirects or fails
- **WHEN** a scheduler request receives a redirect or a status outside the contract's success set
- **THEN** the Job records a content-free failure without following the redirect or retaining the response body

#### Scenario: A functional job remains disabled
- **WHEN** the deployment has not enabled a functional observation job
- **THEN** that job has no scheduled workload or missing-run alert
