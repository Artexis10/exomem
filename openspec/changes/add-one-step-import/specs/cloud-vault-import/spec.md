## ADDED Requirements

### Requirement: The CLI files each landed window

`exomem cloud import` SHALL call the cell's `file` action on each staged window that has landed, once while it waits for room to send the next window and repeatedly after the last window lands, and SHALL report each window that finishes with its filed and failed counts. A reply that stops SHALL end filing for that window and SHALL be reported with its code. A stop for free space SHALL say to free space on the cell and run the command again, and the next run SHALL carry on from the window's saved state. A window that makes no progress over five calls SHALL stay staged and be reported. A cell that refuses `file` SHALL make the command stop filing for the run, say so, and leave the staged windows for the assistant. The `--no-file` flag SHALL leave every window unfiled, and `--collection` SHALL name the collection that filing keeps.

#### Scenario: Windows are filed while the series waits

- **WHEN** a window lands and the next window waits for room on the cell
- **THEN** the command files the landed window before it sleeps and reads the budget again

#### Scenario: An older cell refuses the file call

- **WHEN** the cell refuses `file`
- **THEN** the command files nothing for the rest of the run, reports that the windows wait for the assistant, and does not fail the import

#### Scenario: A window that stopped for space resumes on a rerun

- **WHEN** filing a window stopped for free space and the command runs again
- **THEN** the command seeds that window from the cell's report of its unfiled members and carries on filing it
