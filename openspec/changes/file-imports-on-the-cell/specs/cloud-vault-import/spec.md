## ADDED Requirements

### Requirement: The CLI asks the cell to file and states the origin

`exomem cloud import` SHALL write `"filing": "cell"` into each window's series file unless `--no-file` is given, and SHALL write an `origin` envelope with the browser family `cli` and, when `--label` is given, that label. Each archive member SHALL carry the source file's modification time. A cell with the import filer SHALL finish filing each landed window after the command exits. The command's own `file` calls SHALL keep their behaviour.

#### Scenario: The command exits before filing is done

- **WHEN** `exomem cloud import` uploads a folder and exits while the cell is still filing
- **THEN** the cell finishes filing every window, and each Source carries the stated origin and its file's modification time

#### Scenario: The person declines filing

- **WHEN** the command runs with `--no-file`
- **THEN** no window's series file asks the cell to file, and the windows stay staged for the assistant
