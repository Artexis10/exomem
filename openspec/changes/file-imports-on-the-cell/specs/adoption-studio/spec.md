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

After each filer call the cell SHALL write one filing record per stage. `adoption_studio status` with the stage `path` and `view="filing"` SHALL return its `state`, `as_of`, `progress`, `stop`, `needs`, `person` and `rate` without reading runs, inventories or outcomes. `progress` SHALL carry `total`, `filed`, `skipped_duplicates`, `failed`, `left_out` and `remaining`. `state` SHALL be `working`, `parked`, `done` or `discarded`. The cell SHALL NOT state a time left.

#### Scenario: Home polls a large import

- **WHEN** a client reads the filing view of a stage with tens of thousands of files every few seconds
- **THEN** each read returns the latest record and its cost does not grow with the number of files or runs

#### Scenario: A stage that the filer has not seen

- **WHEN** a client reads the filing view of a stage with no filing record
- **THEN** the reply says `state` `unknown`, `progress` null and no `rate`, and reports no zero counts

#### Scenario: A stage with no manifest has no total

- **WHEN** the stage's receipt lists no members and a filer call has finished
- **THEN** `progress` carries the call's counts, `total` is null and `remaining` is null

### Requirement: A finished stage lists what it left out

A `done` filing view SHALL carry `left_out`: the number of left-out members by reason code, up to 100 of them as `{path, code}` in stable order, and `truncated` true when more exist. The filer SHALL read them once, when it sees `done`, and store them in the record. `progress.left_out` SHALL be that count, and `remaining` SHALL count each member once.

#### Scenario: A stage with hidden files finishes

- **WHEN** a stage with two hidden files and three eligible files is filed
- **THEN** the filing view says `done`, `left_out.by_reason` counts the two hidden files, `items` lists their paths and codes, and `progress.remaining` is 0

#### Scenario: Thousands of files are left out

- **WHEN** a stage leaves out more than 100 members
- **THEN** `left_out.items` lists 100 of them, `truncated` is true and `by_reason` counts all of them

### Requirement: A stop states what it needs

The filing view's `needs` SHALL be a list of objects, empty unless the stage is `parked`. A free-space stop SHALL give `{need: "free_space", bytes}` with the bytes that the next chunk and the reserve need when the stop knows them, else null. An ended login SHALL give `{need: "login"}`. Any other stop SHALL give `{need: "support"}`.

#### Scenario: Filing stops for lack of room

- **WHEN** the next chunk and the reserve do not fit the free space
- **THEN** `needs` holds `free_space` with the bytes that the volume must hold

### Requirement: A rate covers filed work only

The filer SHALL sample the files filed when it registers a stage that nothing has filed, and after each call. The filing view SHALL omit `rate` until two samples exist and their window covers filed work, and SHALL NOT report 0.0 for a stage that filed work.

#### Scenario: A stage that one call filed

- **WHEN** a stage that the cell registered fresh is filed in one call
- **THEN** the filing view carries a `rate` above 0

#### Scenario: One sample is no window

- **WHEN** a foreground `file` call registered the stage and one filer call has finished
- **THEN** the filing view carries no `rate`

### Requirement: A file call with wait false registers without filing

`adoption_studio` `file` with `wait` false and the stage `path` SHALL register the stage with the filer under the caller's login, replacing an ended one. It SHALL make no `file` call in the request, SHALL NOT wait for a running filing call, and SHALL return the filing view. It SHALL refuse a login that the cell cannot replay, and a server with no filer.

#### Scenario: Home wakes a stage whose login ended

- **WHEN** a stage is `parked` with a `login` need and Home calls `file` with `wait` false from a valid login
- **THEN** the call returns the filing view as `working` with no `login` need, and the filer's thread files the stage

#### Scenario: A delegated session cannot register a stage

- **WHEN** a delegated authorization session calls `file` with `wait` false
- **THEN** the call refuses with `IMPORT_FILING_LOGIN_NOT_REPLAYABLE` and the stage stays unregistered

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
