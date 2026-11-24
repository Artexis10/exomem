## ADDED Requirements

### Requirement: Real-model sessions play the storage cases for managed structure
The incident regression suite SHALL play real-model sessions on disposable vaults in invented domains, through a client with hooks and a hookless client. Its cases SHALL cover a later fact that needs a new key, and two overlapping lists whose shared rows agree and merge, each on a file-only vault and a store vault.

#### Scenario: The suite plays each storage case on each client
- **WHEN** the suite runs the new-key and the agreeing-merge cases
- **THEN** each runs on a file-only vault and a store vault, through a client with hooks and a hookless client

### Requirement: Real-model sessions play the cases where the user decides
The suite's cases SHALL also cover two overlapping lists whose shared rows disagree, a new detail that matters to the user, a structure the user designs, an undo the user asks for, and the same need under `advisory` and under `off` capture.

#### Scenario: A disagreement produces one plain question
- **WHEN** the suite plays the disagreeing-lists case
- **THEN** the case passes only if the session asked exactly one question about the user's things before any merge, and the answer is applied with origin `user_choice`

#### Scenario: Off capture restructures nothing
- **WHEN** the suite plays the new-key case under `off` capture
- **THEN** the case passes only if the vault's collections keep their shape

### Requirement: The suite checks the vault and grades the replies
The suite SHALL check the outcome in the vault: the shape history holds the expected step and origin, every seeded value is still readable through the recorded mapping, and a retired collection still holds its rows. A reply meant for the user SHALL fail on any vault path. A blind judge SHALL grade whether each such reply names the user's things in their terms with no internal names, how many questions the session asked, and whether each was about the user's things.

#### Scenario: A later fact is kept without loss or a question
- **WHEN** the suite plays the new-key case under `silent` capture
- **THEN** the case passes only if every earlier row survives and the new occurrence is its own row
- **AND** the session asked no question, and no reply names a vault path or an internal name

### Requirement: The run compares both heads and records each paid run
The run SHALL compare the parent change's head with this change on the same cases, and SHALL record each paid run before any number is quoted.

#### Scenario: A paid run is recorded before a number is quoted
- **WHEN** a paid run of the suite finishes
- **THEN** its record exists before any figure from it is reported, and it names both heads and the cases
