## ADDED Requirements

### Requirement: Fenced governance migration does not depend on a renewable attestation window

A governance migration operating on a fenced custody generation SHALL proceed on the authenticated bundle whether or not its attestation window is still open. Only a serving replica of that generation can renew the window, and a generation that has reached migration is drained, so the system MUST NOT require an open window in the inspect, prepare or enroll phases, and MUST NOT strand a cell whose recovery outlived one attestation lifetime.

Every other precondition remains a refusal. The migration SHALL require a signing keyring valid at the current time, an authentic control MAC, the exact expected custody revision, a control not issued in the future, and a replica proven draining, issuance-stopped and free of in-flight work.

The target-image migration Job reads never-enrolled custody under an open window. Before an inspect or prepare Job, the system SHALL therefore reissue the window of a drained, never-enrolled source whose window has closed or cannot cover one migration Job, as a custody publication of its own, and only while no migration Job holds the fixed Job slot. The reissued generation MUST stay draining with issuance stopped, MUST keep its keyring, and MUST NOT outlast its signing key. The prepared plan binds the window, so the system MUST NOT reissue it under a prepared plan: an enrollment that finds a never-enrolled window too short SHALL return to prepare, reissue there and derive a fresh plan. Returning a drained generation to service still requires a separately authorized resume.

#### Scenario: Recovery resumes after the window closes

- **WHEN** a fenced, never-enrolled generation is requeued at a retained governance checkpoint before prepare, after its attestation window has closed
- **THEN** the system reissues the drained window before starting any migration Job
- **AND** the migration inspects, prepares, enrolls and commits against that generation
- **AND** the generation keeps its keyring and stays draining with issuance stopped

#### Scenario: Enrollment under a closed window returns to prepare

- **WHEN** the window of a never-enrolled generation closes after its plan was prepared
- **THEN** the system returns to prepare without writing custody or starting a Job
- **AND** reissues the window there, prepares a fresh plan, enrolls and commits it

#### Scenario: Enrolled generation keeps its window

- **WHEN** the window of an enrolled generation closes before commit
- **THEN** the system does not reissue it and commits with the window preserved exactly as signed

#### Scenario: Reissue waits for an occupied Job slot

- **WHEN** a migration Job bound to the current custody revision still holds the fixed Job slot
- **THEN** the system resumes that Job and reissues the window only once the slot is empty

#### Scenario: Closed window does not weaken the remaining proofs

- **WHEN** the same closed-window generation presents an expired signing key, a signing key ending before a migration Job could finish, a control issued in the future, a custody revision other than the expected one, or a replica that is not fully drained
- **THEN** the migration refuses before creating any Job or writing any custody bundle

#### Scenario: Serving generation is still refused

- **WHEN** enrollment is attempted against a generation that is still serving
- **THEN** the system refuses, because enrollment records a drained generation and never makes one serving
