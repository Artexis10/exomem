## ADDED Requirements

### Requirement: The CLI files each landed window

`exomem cloud import` SHALL call the cell's `file` action on each staged window that has landed, once while it waits for room to send the next window and repeatedly after the last window lands, and SHALL report each window that finishes with its filed and failed counts. A reply that stops, or a `file` call that the cell refuses with an error, SHALL end filing for that window in this run and SHALL be reported with the cell's code and reason. The command SHALL repeat the exact call once after `MUTATION_OUTCOME_UNKNOWN` or `MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN`, and after a wait while the cell answers `MUTATION_ACKNOWLEDGEMENT_PENDING`. A stop for free space SHALL say to free space on the cell and run the command again, and the next run SHALL carry on from the window's saved state. A window whose replies move no count and leave the page's position unchanged over five calls SHALL stay staged and be reported. Only a refusal of the action itself SHALL count as a cell that refuses `file`: the code `INVALID_MODE`, or a tool error from a cell whose published `import_memory` schema lists its kinds without `file`. Such a cell SHALL make the command stop filing for the run, say so, and leave the staged windows for the assistant. A rerun SHALL skip the windows whose filing the cell reports done. The `--no-file` flag SHALL leave every window unfiled, and `--collection` SHALL name the collection that filing keeps.

#### Scenario: Windows are filed while the series waits

- **WHEN** a window lands and the next window waits for room on the cell
- **THEN** the command files the landed window before it sleeps and reads the budget again

#### Scenario: An older cell refuses the file call

- **WHEN** the cell refuses `file`
- **THEN** the command files nothing for the rest of the run, reports that the windows wait for the assistant, and does not fail the import

#### Scenario: A cell error stops one window with its code

- **WHEN** the cell refuses a `file` call with an error code such as `MUTATION_BUSY`
- **THEN** the command reports that window's stop with the code and the reason, keeps the window staged, and does not say that the cell lacks `file`

#### Scenario: A window that stopped for space resumes on a rerun

- **WHEN** filing a window stopped for free space and the command runs again
- **THEN** the command seeds that window from the cell's report of its unfiled members and carries on filing it
