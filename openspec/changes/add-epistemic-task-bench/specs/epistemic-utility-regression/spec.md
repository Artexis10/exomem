## ADDED Requirements

### Requirement: The epistemic task suite measures the whole memory loop
Family f38 SHALL measure task outcome when the actor builds its own memory over a project's work sessions while the project's knowledge changes. Every probe SHALL depend on one change, and every probe SHALL name one epistemic dimension. The arms N, F, P and E SHALL share every condition except the memory store, and arm F SHALL receive the same learning instruction as arm P, pointed at its folder. The runner SHALL advance all arms one session at a time, and a decision run SHALL play the schedule in two windows at least six hours apart. Each probe SHALL be scored by its key where the key holds one value, and by a blind judge for meaning and behaviour. The report SHALL give per-dimension rows, per-window scores and the disagreements between key and judge.

#### Scenario: A reversed decision is used as it stands now
- **WHEN** session 4 records that staging deploys need one approval and session 7 reverses it to two
- **AND** session 10 asks the actor to prepare a staging deploy
- **THEN** the probe is correct only when the actor requires two approvals
- **AND** the report files the probe under the currency dimension

#### Scenario: Arms cannot drift apart in time
- **WHEN** a decision run plays session 5
- **THEN** no arm starts session 6 before every arm has finished session 5
- **AND** the report gives each arm's score in each window and the change between windows

#### Scenario: A correct answer in other words is not lost
- **WHEN** the key holds "two approvals" and the answer says "a second reviewer must sign off"
- **THEN** the key match fails and the judge marks the answer correct
- **AND** the report lists the probe among the key and judge disagreements
