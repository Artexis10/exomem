## ADDED Requirements

### Requirement: The CLI asks the cell to file and states the origin

`exomem cloud import` SHALL write `"filing": "cell"` into each window's series file unless `--no-file` is given, and SHALL write an `origin` envelope with the browser family `cli` and, when `--label` is given, that label. Each archive member SHALL carry the source file's modification time. A cell with the import filer SHALL finish filing each landed window after the command exits. The command's own `file` calls SHALL keep their behaviour.

#### Scenario: The command exits before filing is done

- **WHEN** `exomem cloud import` uploads a folder and exits while the cell is still filing
- **THEN** the cell finishes filing every window, and each Source carries the stated origin and its file's modification time

#### Scenario: The person declines filing

- **WHEN** the command runs with `--no-file`
- **THEN** no window's series file asks the cell to file, and the windows stay staged for the assistant

### Requirement: The CLI can ask the cell to file and not wait

`exomem cloud import --no-wait` SHALL write the same series file as the default run, with `"filing": "cell"` and the origin envelope, and SHALL make no `file` call. It SHALL refuse `--no-file` and `--collection` with it.

#### Scenario: A sender files nothing itself

- **WHEN** `exomem cloud import --no-wait` uploads a folder
- **THEN** each window's series file asks the cell to file, the command makes no `file` call, and the cell files the stage

### Requirement: A folder with no window budget goes as one import that the cell files

A cell that reports no window budget gets the folder as one import. The import SHALL carry the series file of a window when the cell offers filing and the command asks it to file or states a label. When the command asks the cell to file, the result line SHALL say `"cell_filing": "asked"`. A cell that does not offer filing SHALL get a plain import. When the command asks that cell to file, the line SHALL say `"cell_filing": "not_offered"`.

#### Scenario: A cell with no window budget

- **WHEN** `exomem cloud import --no-wait` uploads a folder to a cell that reports no window budget and offers filing
- **THEN** the one import carries the series file that asks the cell to file, and the result line says `"cell_filing": "asked"`

#### Scenario: A cell that cannot file

- **WHEN** `exomem cloud import --label X` uploads a folder to a cell that reports no window budget and does not offer filing
- **THEN** the import is plain, the command reports that the cell cannot record the label, the result line says `"cell_filing": "not_offered"`, and the result does not claim that the cell will file it

### Requirement: A resumed one-import run keeps the choices of its first run

The checkpoint of a one-import run SHALL record whether its series file asks the cell to file, its label and its collection. A rerun that gives other values SHALL stop with `CHECKPOINT_OPTIONS_CHANGED`, name the flag that differs and keep the checkpoint. A rerun that matches SHALL report the recorded `cell_filing`. A checkpoint with no record SHALL count as a plain import.

#### Scenario: A rerun changes what the series file carries

- **WHEN** a one-import run stops after it declared its import with `--no-file`, and the rerun uses the default flags
- **THEN** the command stops with `CHECKPOINT_OPTIONS_CHANGED`, names `--no-file`, and keeps the checkpoint

#### Scenario: A rerun matches the first run

- **WHEN** a one-import run that asked the cell to file stops, and the rerun uses the same flags
- **THEN** the import lands with the series file of the first run, and the result line says `"cell_filing": "asked"`
