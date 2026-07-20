## MODIFIED Requirements

### Requirement: Preferred Writer Reclaim

A replica configured as the preferred writer SHALL continue attempting to acquire writer authority for as long as it holds no lease, rather than attempting once at startup. Reclaim MUST NOT displace a live holder: authority is granted only when the existing lease is absent or expired. A replica not configured as preferred MUST NOT promote itself; it acquires authority only when a mutation reaches it.

#### Scenario: Preferred replica loses the startup race

- **WHEN** a preferred replica starts while another replica holds a live lease
- **THEN** its startup acquisition fails without terminating startup
- **AND** it continues attempting acquisition on the renew cadence rather than remaining a follower for the process lifetime

#### Scenario: Previous holder stops renewing

- **WHEN** the replica holding the lease stops renewing and the lease expires
- **THEN** the preferred replica acquires writer authority on a subsequent attempt
- **AND** edge routing follows the new holder without operator intervention

#### Scenario: Reclaim never preempts a live holder

- **WHEN** a preferred replica repeatedly attempts acquisition while another replica holds an unexpired lease
- **THEN** every attempt is refused
- **AND** the existing holder retains authority and its fencing token is unchanged

#### Scenario: Non-preferred follower does not self-promote

- **WHEN** a replica without the preferred-writer setting holds no lease
- **THEN** it makes no periodic acquisition attempt
- **AND** it becomes writer only when a mutation reaching it acquires authority
