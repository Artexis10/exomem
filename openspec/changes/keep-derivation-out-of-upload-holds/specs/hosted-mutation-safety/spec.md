## ADDED Requirements

### Requirement: Upload commits keep derived fan-out out of the boundary hold

An upload commit SHALL hold the vault mutation boundary only to plan, install its stage, and write its pages, navigation and graph epoch. It SHALL record durable full-refresh custody for its pages before the install. Lexical, resolver, graph and embedding fan-out SHALL run after the release. The fan-out SHALL apply the recall admission of the bytes it reads. The upload receipt SHALL state whether the fan-out completed.

#### Scenario: A note save during a slow upload fan-out does not wait

- **WHEN** an upload commit's derived fan-out is slow
- **AND** a note save asks for the vault mutation boundary while that fan-out runs
- **THEN** the note save acquires the boundary without waiting for the fan-out
- **AND** the upload's boundary hold does not grow with the fan-out's duration
- **AND** the upload receipt states that the fan-out completed

#### Scenario: A stop between install and fan-out leaves a receipt that the next drain finishes

- **WHEN** an upload commit installs its pages
- **AND** the process stops after the release and before the derived fan-out completes
- **THEN** the pages keep their durable full-refresh custody
- **AND** the upload receipt, if one was sent, states that the fan-out is pending
- **AND** the next drain refreshes the pages so that they become searchable, and retires their custody
