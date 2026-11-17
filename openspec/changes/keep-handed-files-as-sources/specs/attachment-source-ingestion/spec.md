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
