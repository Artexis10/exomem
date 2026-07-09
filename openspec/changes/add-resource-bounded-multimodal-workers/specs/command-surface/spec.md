## ADDED Requirements

### Requirement: Media runtime appears in resource diagnostics
`exomem status --resources` SHALL include a stable media-runtime object with queue counts,
worker-active state, worker PID when safely known, idle timeout, and job-store health. Doctor
SHALL report blocked media jobs and profile remediation without loading a model.

#### Scenario: JSON status remains no-allocation
- **WHEN** resource status is requested before any media job has run
- **THEN** it reports the media runtime and semantic deferred-work state
- **AND** it does not create a job DB, load a model, or initialize an accelerator

#### Scenario: Doctor finds blocked jobs
- **WHEN** durable media jobs are blocked by a missing optional engine
- **THEN** doctor reports a warning with the blocked count and remediation
- **AND** the overall core service can still pass readiness
