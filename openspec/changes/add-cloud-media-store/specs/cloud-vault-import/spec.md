## ADDED Requirements

### Requirement: An import larger than the cell's volume arrives in windows

A sender SHALL split a folder larger than the cell's window budget into windows, where each window:
- is an ordinary `adopt` import with its own import ID and recipient;
- starts only when the budget that the cell reports covers it;
- holds store-bound members before resident members, in path order across the series;
- needs at most the budget, counted as twice its resident bytes plus its store-bound bytes plus its largest store-bound member.

The sender is the Cloud CLI or the Home page.

#### Scenario: A folder of three times the budget lands without growth

- **WHEN** a sender imports a folder of recordings and notes that holds three times the window budget
- **THEN** it creates three or more windows, one at a time
- **AND** the cell's volume use stays at or below its size less the reserve
- **AND** the cell's storage size does not change

#### Scenario: A member cannot fit an empty budget

- **WHEN** a window of one store-bound member alone would need more than the budget that the cell would report with no staged data
- **THEN** the sender refuses the series before any upload and reports the window's need and the budget

#### Scenario: The volume has not drained

- **WHEN** the next window needs more than the budget that the cell reports
- **THEN** the sender creates no import, waits, and reports the shortfall
- **AND** it creates the window once the reported budget covers it

#### Scenario: The Job refuses a window for space

- **WHEN** the import Job refuses a window for lack of space
- **THEN** the sender plans that window again from the current budget
- **AND** it sends no landed window again

#### Scenario: A series larger than the remaining allowance

- **WHEN** the series' store-bound bytes exceed the remaining media allowance that the cell reports
- **THEN** the sender refuses the series before any upload and reports both figures

#### Scenario: An older cell gets one import

- **WHEN** the cell reports no window budget
- **THEN** the sender creates one import for the folder, as before

### Requirement: A series resumes at its first unlanded window

A sender SHALL record each series' partition: every member's path, size and modification time, and each window's state. A rerun then resumes at the first window that has not landed and sends no landed window again.

#### Scenario: A restart resumes at the third window

- **WHEN** the sender stops after window 2 lands, and the person imports the same folder again
- **THEN** the sender creates window 3
- **AND** it sends no byte of windows 1 and 2

### Requirement: The sender and the cell classify members by one rule

Every sender, and the cell's stage offload and adoption inventory, SHALL classify a member as store-bound or resident by the one rule that the cell reports. The store bytes that a sender declares then equal the bytes that stage offload stores.

#### Scenario: Declared and stored bytes agree

- **WHEN** a generated folder holds recordings, notes, a text file above the inline text limit and a file above the per-object bound
- **THEN** the sender's declared store bytes equal the bytes that stage offload stores for it

### Requirement: The cell reports its window budget

The Adoption Studio status SHALL report these figures, and each figure that the cell cannot read SHALL be unknown with a reason, never zero:
- the window budget;
- the store's per-object bound and the remaining media allowance;
- the inputs of the store-bound rule;
- for each stage, the count and bytes of its stored, local and missing members.

The window budget is free space less the reserve, the temporary-space bound, the unfiled staged resident bytes and the largest local store-bound staged member.

#### Scenario: The budget is computed from its inputs

- **WHEN** the volume has 40 GiB free, the reserve is 0.25 GiB, the temporary-space bound is 2 GiB, unfiled staged resident bytes are 3 GiB and the largest local store-bound staged member is 1 GiB
- **THEN** the status reports a window budget of 33.75 GiB

#### Scenario: An input cannot be read

- **WHEN** the cell cannot read the volume's free space
- **THEN** the status reports the window budget as unknown with a reason, not as zero

#### Scenario: A stage is draining

- **WHEN** a stage holds 20 stored members, 3 local members and 1 missing member
- **THEN** the status gives the count and bytes of each state for that stage
