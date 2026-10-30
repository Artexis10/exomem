## MODIFIED Requirements

### Requirement: Readiness reflects takeover safety
Standalone Exomem SHALL remain ready without multi-host coordination. A coordinated replica SHALL report takeover eligibility only when its coordinator is healthy, its replica identity is configured, and its role is authoritatively known. A served recall-catalogue lag, which `install-readiness` defines, SHALL NOT change takeover eligibility and SHALL NOT add a reason code.

#### Scenario: Healthy coordinated follower
- **WHEN** a follower can confirm the current lease state
- **THEN** readiness returns 200 and reports takeover eligibility true

#### Scenario: Coordinator is unavailable
- **WHEN** a coordinated replica cannot confirm lease state
- **THEN** readiness returns 503 and reports takeover eligibility false

#### Scenario: A served catalogue lag leaves takeover eligibility unchanged
- **WHEN** a proven replica with a healthy coordinator reports its recall catalogue as `lagging`
- **THEN** readiness returns 200, reports takeover eligibility true, and lists no reason codes
