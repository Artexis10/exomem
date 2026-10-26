## ADDED Requirements

### Requirement: The Cloud CLI needs no endpoint setup and reports its phases

`exomem cloud import` and `exomem cloud export` SHALL default their MCP and API endpoints to the production Exomem Cloud endpoints, defined once in the product. The `--mcp-url` and `--api-base-url` flags and the `EXOMEM_CLOUD_MCP_URL` and `EXOMEM_CLOUD_API_BASE_URL` variables SHALL override the defaults.

`exomem cloud import` SHALL select files with the same rules as `exomem import`: hidden entries and the selected folder's `.gitignore` rules are left behind by default, `--include-hidden` and `--no-gitignore` opt back in, and a symbolic link, special file or unreadable file is left behind with its reason. The command SHALL NOT follow a symbolic link, and SHALL NOT stop the archive because one exists. Before it builds the archive, it SHALL report what it leaves behind, with the count and bytes under each reason, and then the selection's file count and bytes. It SHALL refuse with the measured values when `age` is not available or when the free space where it writes the ciphertext is smaller than the ciphertext's expected size. The client SHALL NOT carry the cell's member, path or name limits; the cell enforces them.

The command SHALL report progress for each phase on standard error: archive and encryption (bytes written), upload (parts and bytes sent), and server import (state). Every failure SHALL name its phase, a content-free reason code and the import id when one exists, and SHALL NOT carry a file name or a presigned address. The final standard-output line SHALL stay one JSON object, and later versions SHALL only add fields to it. Resume SHALL keep using the existing checkpoint.

When the command ends with the ciphertext still on the disk, it SHALL report those bytes, state that they duplicate the user's data until the import finishes, and name the discard route that exists.

#### Scenario: An import runs with no environment set

- **WHEN** a user runs `exomem cloud import ~/Notes` with neither endpoint variable set
- **THEN** the command uses the production endpoints and reports what it leaves behind, then the selected count and bytes, before encrypting

#### Scenario: A symbolic link is skipped and reported

- **WHEN** the folder holds a symbolic link, at the top or inside a subfolder
- **THEN** the archive holds no entry for the link and nothing it points to, and the report counts the link under the symbolic link reason

#### Scenario: Too little disk for the ciphertext is refused before encrypting

- **WHEN** the selection's bytes exceed the free space where the ciphertext is written
- **THEN** the command refuses with the expected size and the free space, and writes no ciphertext

#### Scenario: A failed upload says where it stopped

- **WHEN** the upload phase fails after some parts are sent
- **THEN** the command reports the upload phase, a reason code and the import id, and a rerun resumes from the checkpoint

#### Scenario: The kept ciphertext is named as a duplicate

- **WHEN** the command ends and the encrypted copy remains in the checkpoint folder
- **THEN** it reports the copy's bytes, says that it duplicates the data until the import finishes, and names how to discard it
