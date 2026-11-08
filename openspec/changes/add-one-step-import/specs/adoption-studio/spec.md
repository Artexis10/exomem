## ADDED Requirements

### Requirement: One call files a folder

`adoption_studio` SHALL accept the action `file`, public as `import_memory` with operation kind `file`. It SHALL take the staged folder `_Imports/<id>` as `path`, an optional `collection` and an optional `continuation`, and SHALL refuse any other folder with `IMPORT_REQUEST_INVALID`. Each call SHALL run the existing `start`, `select`, `plan`, `apply` and `finish` functions, chosen from the saved phase of the newest unfinished page, and SHALL write nothing that those functions do not write. A call SHALL file every page of the folder when the agent repeats it with the `continuation` it returns.

A call SHALL carry on the run that issued its `continuation`. A call with only a `path` SHALL carry on the newest unfinished run of that path and SHALL NOT start a second run. A `continuation` that no `file` call issued, or that belongs to another path or collection, SHALL be refused with `INVALID_CONTINUATION`. A page with nothing eligible SHALL be cancelled so that the scan reaches the next page.

The reply SHALL carry `run_id`, `filing`, `progress`, `summary`, `person` and, while a continuation exists, `continuation`. `filing` SHALL be `working`, `stopped` or `done`. A stopped reply SHALL carry `stop` with a code and a reason. `summary` SHALL state only the filed, failed and left counts. Only a `done` reply SHALL carry `filed`, which reports what `finish` verified. A `working` reply and a stopped space reply SHALL carry a fresh `continuation` each time. The outcome sentence of a `file` call SHALL NOT say that files were saved.

A chunk that does not fit the free space SHALL stop the reply with the apply stop and a `free_space` need for the person. An error after a run exists SHALL stop the reply with that error's code and SHALL keep the committed work in `progress`. A `done` reply SHALL list in `person` the review and discard that the residual calls for. The step actions SHALL keep their behavior.

#### Scenario: An agent files a multi-page folder by repeating one call

- **WHEN** an agent calls `file` on a staged folder of several pages and repeats it with each returned `continuation` until `filing` is not `working`
- **THEN** every eligible file is filed once, the last reply reports `filing` `done` with `progress.failed` 0 and the `finish` result in `filed`, and no reply carries a plan id or a page cursor

#### Scenario: A call with only the path carries on

- **WHEN** a first `file` call is repeated with only the `path`, or a step-path run of that folder is unfinished
- **THEN** the call carries on the newest unfinished run and starts no second run

#### Scenario: A full volume stops the import and says what the person does

- **WHEN** the next chunk does not fit the free space
- **THEN** the reply reports `filing` `stopped` with the bytes needed and free, a `free_space` need and a `continuation`, and the files already filed stay reported

#### Scenario: A foreign continuation is refused

- **WHEN** a call passes a `continuation` from another path or collection
- **THEN** the call fails with `INVALID_CONTINUATION` and files nothing
