## ADDED Requirements

### Requirement: One call files a folder

`adoption_studio` SHALL accept the action `file`, public as `import_memory` with operation kind `file`. It SHALL take the staged folder `_Imports/<id>` as `path`, an optional `collection` and an optional `continuation`, and SHALL refuse any other folder with `IMPORT_REQUEST_INVALID`. Each call SHALL run the existing `start`, `select`, `plan`, `apply` and `finish` functions, chosen from the saved phase of the newest unfinished page, and SHALL write nothing that those functions do not write. A call SHALL file every page of the folder when the agent repeats it with the `continuation` it returns.

A call SHALL carry on the run that issued its `continuation`. A call with only a `path` SHALL carry on the newest run of that path that has a step left, else the newest run of that path, and SHALL NOT start a second run. Such a call on a finished import SHALL answer `done` again, also when finishing removed the stage, while no staged file under the path is one that no page of the scan classified (`outside_run` in the residual). A hidden file SHALL NOT count, because a scan skips it. When such a file exists, the call SHALL start a new scan of the path under the import's collection, and the `filing_done` that `adoption_studio status` reports SHALL follow the same rule. A `continuation` that no `file` call issued, or that belongs to another path or collection, SHALL be refused with `INVALID_CONTINUATION`. A call with only a `path` that names a `collection` other than the run's SHALL be refused with `IMPORT_REQUEST_INVALID`, and the reason SHALL name the run's collection. A page with nothing eligible SHALL be cancelled so that the scan reaches the next page.

A page whose planned files an `apply` limited by `only_paths` left without an outcome SHALL apply them before it retries or finishes. A page with failed files SHALL retry them once with `apply`. The page SHALL then finish, or be cancelled when every file failed, and the scan SHALL go on to the next page. The failed files SHALL stay counted in `progress.failed` and listed in the residual. A `file` call SHALL stop only for free space, an unresolved recovery or an error.

The reply SHALL carry `run_id`, `collection`, `filing`, `progress`, `summary`, `person` and, while a continuation exists, `continuation`. `filing` SHALL be `working`, `stopped` or `done`. A stopped reply SHALL carry `stop` with a code and a reason. `progress` SHALL count the whole import and SHALL carry the newest page's position: its number, its phase and the files planned so far. `summary` SHALL state only the filed, failed and left counts of the whole import. Only a `done` reply SHALL carry `filed`, which holds the stage's residual as the stage is when the reply is built and, under `last_finished_page` with that page's number, what the last `finish` verified about that page alone. A `done` reply SHALL NOT present one page's coverage as the import's. A `working` reply and a stopped space reply SHALL carry a fresh `continuation` each time. The outcome sentence of a `file` call SHALL NOT say that files were saved.

A chunk that does not fit the free space SHALL stop the reply with the apply stop and a `free_space` need for the person. An error after a run exists SHALL stop the reply with that error's code and SHALL keep the committed work in `progress`. A `done` reply SHALL list in `person` the review and discard that the residual calls for. The step actions SHALL keep their behavior.

#### Scenario: An agent files a multi-page folder by repeating one call

- **WHEN** an agent calls `file` on a staged folder of several pages and repeats it with each returned `continuation` until `filing` is not `working`
- **THEN** every eligible file is filed once, the last reply reports `filing` `done` with `progress.failed` 0 and the last `finish` result under `filed.last_finished_page`, and no reply carries a plan id or a page cursor

#### Scenario: A call with only the path carries on

- **WHEN** a first `file` call is repeated with only the `path`, or a step-path run of that folder is unfinished
- **THEN** the call carries on the newest unfinished run and starts no second run

#### Scenario: A page whose files fail does not stop the import

- **WHEN** files on one page fail to file, and still fail when that page retries them once
- **THEN** the later pages are filed, the last reply reports `filing` `done`, and `progress.failed`, `summary` and the residual count the failed files

#### Scenario: A call with only the path after done answers done

- **WHEN** an import is done, with or without a residual, no file came into its stage after its scan, and a `file` call names only its `path`
- **THEN** the call answers `done` with the same counts and starts no run

#### Scenario: A file staged after done is filed

- **WHEN** an import is done, a rerun of a local import or a person puts a file in its stage, and a `file` call names only its `path`
- **THEN** the call starts a new scan under the import's collection, files that file, and files no file twice

#### Scenario: A hidden file staged after done starts no scan

- **WHEN** an import is done, a hidden file appears in its stage, and a `file` call names only its `path`
- **THEN** the call answers `done`, starts no run, and its residual lists the hidden file as `outside_run`

#### Scenario: A page that an apply of some files left is filed whole

- **WHEN** a step-path `apply` with `only_paths` leaves planned files of a page without an outcome, and a `file` call carries on that page
- **THEN** the call applies those files before it finishes the page, and every planned file is filed

#### Scenario: The done reply keeps page coverage apart from the import's totals

- **WHEN** an import of three pages is done and its last page finished nothing
- **THEN** `progress` and `summary` count every page, and `filed.last_finished_page` names the page whose `finish` it reports

#### Scenario: A call with only the path names another collection

- **WHEN** an import of a path runs as one collection and a call with only that path names another
- **THEN** the call fails with `IMPORT_REQUEST_INVALID`, its reason names the import's collection, and it files nothing

#### Scenario: A full volume stops the import and says what the person does

- **WHEN** the next chunk does not fit the free space
- **THEN** the reply reports `filing` `stopped` with the bytes needed and free, a `free_space` need and a `continuation`, and the files already filed stay reported

#### Scenario: A foreign continuation is refused

- **WHEN** a call passes a `continuation` from another path or collection
- **THEN** the call fails with `INVALID_CONTINUATION` and files nothing
