## ADDED Requirements

### Requirement: A Taken Name Gets The Next Free Name And No Page Is Replaced

At plan time the system SHALL give each item a page name, and for an artifact an artifact name, that no file in the target folder holds and no earlier item of the plan holds, comparing names without letter case. A name that is taken SHALL get the suffix `-2`, then `-3`, and so on, and adoption SHALL NOT replace or edit a page that exists.

#### Scenario: A planned name is already a page

- **WHEN** the target folder already holds a page with the name that an item would take
- **THEN** the item plans the next free name with the `-2` suffix
- **AND** the existing page keeps its bytes

#### Scenario: Two items of one plan take one name

- **WHEN** two selected notes carry the same title
- **THEN** the first plans the plain name and the second plans the name with the `-2` suffix

### Requirement: A Name The Vault Cannot Keep Is Refused As An Unsafe Path

The system SHALL refuse at plan time an original whose name the vault cannot keep on every platform, with the code `INVALID_PATH`. A backslash in a name, and a folder or file name that ends with a space or a dot, SHALL be refused. The refusal SHALL name the original and SHALL NOT use `NOT_FOUND`, and the other originals of the plan SHALL import.

#### Scenario: A name with a backslash

- **WHEN** a selected file's name holds a backslash
- **THEN** the plan skips it with `INVALID_PATH`
- **AND** the other selected files are planned and import

#### Scenario: A folder name that ends with a space

- **WHEN** a selected file sits in a folder whose name ends with a space
- **THEN** the plan skips it with `INVALID_PATH`

### Requirement: A Text Original That Is Not UTF-8 Is An Artifact With Searchable Text

An original that reads as text but is not valid UTF-8 SHALL import as an artifact with its exact bytes. An original with the suffix `.md` SHALL be stored with the suffix `.md.txt`, outside the graph input set, so that its text is extracted and searchable.

#### Scenario: A Latin-1 Markdown note

- **WHEN** a selected `.md` file holds Latin-1 text that is not valid UTF-8
- **THEN** the item imports as an artifact whose bytes equal the original
- **AND** the artifact name ends with `.md.txt` and has the text extraction kind
- **AND** its Source page keeps the import provenance (`imported_from`, `original_sha256`) after the extraction request
