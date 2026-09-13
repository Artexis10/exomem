## ADDED Requirements

### Requirement: Fenced governance migration does not depend on a renewable attestation window

A governance migration operating on a fenced custody generation SHALL proceed on the authenticated bundle whether or not its attestation window is still open. Only a serving replica of that generation can renew the window, and a generation that has reached migration is drained, so the system MUST NOT require an open window in the inspect, prepare or enroll phases, and MUST NOT strand a cell whose recovery outlived one attestation lifetime.

Every other precondition remains a refusal. The migration SHALL require a signing keyring valid at the current time, an authentic control MAC, the exact expected custody revision, a control not issued in the future, and a replica proven draining, issuance-stopped and free of in-flight work. The migration MUST NOT renew, extend or reissue the window: a closed window stays closed, and returning a drained generation to service still requires a separately authorized resume.

#### Scenario: Recovery resumes after the window closes

- **WHEN** a fenced, never-enrolled generation is requeued at a retained governance checkpoint after its attestation window has closed
- **THEN** the migration inspects, prepares, enrolls and commits against that generation
- **AND** the stored window is preserved exactly as signed rather than renewed

#### Scenario: Closed window does not weaken the remaining proofs

- **WHEN** the same closed-window generation presents an expired signing key, a control issued in the future, a custody revision other than the expected one, or a replica that is not fully drained
- **THEN** the migration refuses before creating any Job or writing any custody bundle

#### Scenario: Serving generation is still refused

- **WHEN** enrollment is attempted against a generation that is still serving
- **THEN** the system refuses, because enrollment records a drained generation and never makes one serving
