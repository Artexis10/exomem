## ADDED Requirements

### Requirement: The cell finishes a registered import by itself

A serving cell SHALL run one import filer that drives the `file` action on each registered stage until filing is `done` or stops. A stage SHALL become registered when its series file states `"filing": "cell"` and it has staged members, or when any `file` call names it. The filer SHALL NOT run in the stdio runtime.

#### Scenario: A staged Cloud import is filed with no assistant

- **WHEN** a Cloud import's series file states `"filing": "cell"`, the import Job stages it and the cell starts
- **THEN** the filer files every eligible file with no `file` call from any client, and the filing record reaches `done`

#### Scenario: An agent's file call registers the stage

- **WHEN** an agent calls `file` once on a stage whose series file does not ask the cell to file
- **THEN** the cell finishes filing that stage without further calls

### Requirement: The filer carries on exactly once

On start, the filer SHALL carry on every registered stage whose filing record is not `done` or `discarded`, from its saved phase. Before each call it SHALL record the call's mutation identity and continuation, and after a crash it SHALL repeat that exact call under the same identity. It SHALL NOT start a second run of a stage. Its calls and foreground `file` calls on one stage SHALL take turns.

#### Scenario: The cell restarts during filing

- **WHEN** the cell restarts while the filer is working on a stage
- **THEN** the filer carries on that stage's run, repeats any call whose outcome is unknown under its recorded identity, and files each eligible file once

#### Scenario: The filer and an agent share a stage

- **WHEN** an agent calls `file` on a stage while the filer is working on it
- **THEN** the two calls run one after the other, and no second run of the stage starts

### Requirement: The filer parks a stopped import

A stop for free space, an unresolved recovery, a repeated busy answer or another error SHALL park the stage with its code and reason. A parked stage SHALL resume on a later `file` call or a restart, and a free-space stop SHALL also resume after a periodic space check finds room.

#### Scenario: Filing stops for free space

- **WHEN** a filer call stops for free space
- **THEN** the filing record shows `parked` with the free-space stop and its `person` need
- **AND** the filer resumes the stage after space is freed and the next space check or `file` call

### Requirement: A filing record answers status at constant cost

After each filer call the cell SHALL write one filing record per stage with `state`, `progress`, `stop`, `person`, `as_of` and, once two calls have finished, `rate` in files per minute. `state` SHALL be `working`, `parked`, `done` or `discarded`. `adoption_studio status` with the stage `path` and `view="filing"` SHALL return that record without reading runs, inventories or outcomes. The cell SHALL NOT state a time left.

#### Scenario: Home polls a large import

- **WHEN** a client reads the filing view of a stage with tens of thousands of files every few seconds
- **THEN** each read returns the latest record and its cost does not grow with the number of files or runs

#### Scenario: A stage that the filer has not seen

- **WHEN** a client reads the filing view of a stage with no filing record
- **THEN** the reply says `state` `unknown` and reports no zero counts

### Requirement: Imported Sources record the origin their uploader stated

When a stage's series file carries an `origin` envelope, filing SHALL write onto each Source of that stage every registered provenance field that has a value: the label, the device family, the browser family, and the member's modification time as an ISO time. A stage without an envelope SHALL get no provenance field. A provenance field SHALL NOT be used as a capture date.

#### Scenario: A phone upload records its origin

- **WHEN** a stage with origin label "Photos from my phone", device family `phone` and browser family `safari` is filed
- **THEN** each Source carries `origin_label`, `upload_device`, `upload_browser` and `file_modified`, beside its existing `imported_from` and `collection_path`

#### Scenario: An owner retires the device field

- **WHEN** the vault overlay retires the upload device entry and a stage with an origin envelope is filed
- **THEN** no Source of that stage carries `upload_device`, and the other provenance fields are written

#### Scenario: An older stage has no origin

- **WHEN** a stage whose series file has no `origin` is filed
- **THEN** its Sources carry no provenance field and no `file_modified`
