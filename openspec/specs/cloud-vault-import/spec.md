# cloud-vault-import Specification

## Purpose

Define how the Cloud CLI selects, checks and transfers an import, and reports its progress and retained copies.

## Requirements

### Requirement: The Cloud CLI needs no endpoint setup and reports its phases

`exomem cloud import` and `exomem cloud export` SHALL default their MCP and API endpoints to the production Exomem Cloud endpoints, defined once in the product. The `--mcp-url` and `--api-base-url` flags and the `EXOMEM_CLOUD_MCP_URL` and `EXOMEM_CLOUD_API_BASE_URL` variables SHALL override the defaults.

`exomem cloud import` SHALL select files with the same rules as `exomem import`: hidden entries and the selected folder's `.gitignore` rules are left behind by default, `--include-hidden` and `--no-gitignore` opt back in, and a symbolic link, Windows junction, special file or unreadable file is left behind with its reason. The command SHALL NOT follow a link, and SHALL NOT stop the archive because one exists. A file that changes while it is read SHALL fail the run with a content-free code instead of entering the archive. Before it builds the archive, it SHALL report what it leaves behind, with the count and bytes under each reason, and then the selection's file count and bytes. It SHALL refuse with the measured values when `age` is not available or when the free space where it writes the ciphertext is smaller than the ciphertext's expected size plus the same reserve as local import. It SHALL count the archive's members, folders included, and refuse before encrypting, with the measured count and the limit, when the count exceeds the cell's member limit. The client SHALL read that limit from the cell's import module and SHALL NOT carry a copy of its value; the cell enforces the path and name limits.

The command SHALL report progress for each phase on standard error: archive and encryption (bytes written), upload (parts and bytes sent), and server import (state). Every failure SHALL name its phase, a content-free reason code and the import id when one exists, and SHALL NOT carry a file name or a presigned address. The final standard-output line SHALL stay one JSON object, and later versions SHALL only add fields to it. Resume SHALL keep using the existing checkpoint. A failure that cannot resume SHALL NOT say to resume: it SHALL remove the checkpoint, say that a rerun starts a new import, and name the routes that stop the open Cloud import, which are Home's Cancel and the `import_cancel` action (an older cell may lack the action). A registration that Cloud refuses because another import is open SHALL NOT say to resume either: it SHALL keep the checkpoint, and say to cancel the open import in Home or wait for it to expire, and then to run the command again.

When the command ends with the ciphertext still on the disk, it SHALL report those bytes, state that they duplicate the user's data until the import finishes, and name the discard route that exists.

#### Scenario: An import runs with no environment set

- **WHEN** a user runs `exomem cloud import ~/Notes` with neither endpoint variable set
- **THEN** the command uses the production endpoints and reports what it leaves behind, then the selected count and bytes, before encrypting

#### Scenario: A symbolic link is skipped and reported

- **WHEN** the folder holds a symbolic link, at the top or inside a subfolder
- **THEN** the archive holds no entry for the link and nothing it points to, and the report counts the link under the symbolic link reason

#### Scenario: A file that changes while it is archived is not archived torn

- **WHEN** a selected file changes between the scan and the end of its read
- **THEN** the command fails with `SOURCE_CHANGED`, removes the checkpoint, says that a rerun starts a new import and names Home's Cancel and `import_cancel` for the open Cloud import

#### Scenario: Too little disk for the ciphertext is refused before encrypting

- **WHEN** the selection's bytes exceed the free space where the ciphertext is written
- **THEN** the command refuses with the expected size and the free space, and writes no ciphertext

#### Scenario: A folder with more entries than the cell accepts is refused before encrypting

- **WHEN** the files and the folders that hold them outnumber the cell's member limit
- **THEN** the command refuses with the measured count and the limit, and writes no ciphertext

#### Scenario: A registration refused for an open import does not say to resume

- **WHEN** Cloud refuses the registration because another import is open
- **THEN** the command says to cancel that import in Home or wait for it to expire, does not say to resume, and a rerun after the open import ends registers the same import

#### Scenario: A failed upload says where it stopped

- **WHEN** the upload phase fails after some parts are sent
- **THEN** the command reports the upload phase, a reason code and the import id, and a rerun resumes from the checkpoint

#### Scenario: The kept ciphertext is named as a duplicate

- **WHEN** the command ends and the encrypted copy remains in the checkpoint folder
- **THEN** it reports the copy's bytes, says that it duplicates the data until the import finishes, and names how to discard it
