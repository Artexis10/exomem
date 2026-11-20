## ADDED Requirements

### Requirement: Receiving a file makes it eligible for custody

A file the user hands the agent to read SHALL be eligible for capture as a Source by its exact bytes, whether or not it holds a durable conclusion. Eligibility SHALL NOT confirm the write: an agent-initiated capture SHALL obey the `proactive_capture` disposition. Files the agent writes or edits in the user's own workspace or repository, and files the user only mentions, SHALL NOT be eligible. A file that is already stored SHALL return `already_stored` and write nothing.

#### Scenario: A repository file is not a handed file
- **WHEN** the user asks the agent to fix a tracked file in the user's repository
- **THEN** the agent writes no Source for that file

#### Scenario: A file the user says to ignore is not kept
- **WHEN** the user names a file in the folder and says to ignore it
- **THEN** the agent writes no Source for that file

### Requirement: A text copy is reported as a text copy

When `save_source` stores supplied text rather than an uploaded original, its outcome line SHALL say that Exomem kept the text, not the original file.

#### Scenario: An agent pastes a transcript's text
- **WHEN** an agent saves a transcript's text through `content` instead of uploading the file
- **THEN** the reply's outcome line says the text was kept and the original file was not

### Requirement: A kept-for-custody Source waits in its own quiet count

`save_source` SHALL accept `options.custody_only: true`, which records `custody_only: true` on the new Source and adds "kept for custody only" to the reply's outcome line. The `unprocessed_source` audit family SHALL skip a Source that records `custody_only: true`. The `custody_source` audit family SHALL list each such Source that no live page compiles, at `info` severity whatever its age, oldest first. The family SHALL NOT be in the default attention set. A Source that a live page compiles SHALL appear in neither family.

#### Scenario: A screenshot kept only for safekeeping
- **WHEN** an agent saves a handed screenshot with `options.custody_only: true`
- **THEN** the reply's outcome line says the file was kept for custody only
- **AND** the Source appears in `custody_source` at `info` severity and not in `unprocessed_source`

#### Scenario: An old custody Source never becomes a warning
- **WHEN** a Source marked `custody_only: true` has waited more than 90 days with no note citing it
- **THEN** its `custody_source` finding stays at `info` severity

#### Scenario: Compiling a custody Source clears it
- **WHEN** a live note cites a Source marked `custody_only: true`
- **THEN** the Source appears in neither `custody_source` nor `unprocessed_source`

### Requirement: A Source upload step names the held send

When `upload_files` with lane `source` issues an unheld upload step, its reply SHALL carry a `next_action` that tells an agent holding the file to call again with `expected_sha256`, so the send is held and `save_source` takes the document's title, kind and url. It SHALL say that a POST to the unheld step stores the file at once under its file name, with no url and no duplicate check.

#### Scenario: An agent asks for an upload without a digest
- **WHEN** an agent calls `upload_files` with lane `source` and no `expected_sha256` on a server with an upload token
- **THEN** the reply carries the upload step and a `next_action` that names `expected_sha256` and `save_source`
