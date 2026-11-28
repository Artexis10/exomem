## MODIFIED Requirements

### Requirement: A folder outside the vault imports through local staging

`adoption_studio start` SHALL refuse a `path` that is absolute, or that climbs out of the vault by `..`, with `IMPORT_PATH_OUTSIDE_VAULT`, and the message SHALL name `evermatter import <folder>`. The check SHALL read the path as text and SHALL NOT resolve it on the filesystem. A path that escapes through a symbolic link inside the vault SHALL get the overview resolver's `INVALID_PATH`.

The `evermatter import <folder>` command SHALL enumerate the folder without following symbolic links, and SHALL select files by these rules:

- A hidden entry (a name that starts with a dot, or the Windows hidden attribute), at any depth, SHALL be left behind unless the user passes `--include-hidden`.
- The `.gitignore` files inside the folder SHALL apply, with git's precedence, unless the user passes `--no-gitignore`. The command SHALL NOT hard-code a folder name to leave behind.
- A symbolic link or a Windows junction at any depth SHALL be left behind and never followed. A special file and an unreadable file SHALL be left behind with their own reasons.

Before it copies, the command SHALL report what it leaves behind, with the count and bytes under each reason, and SHALL report the selected file count and bytes against the free space of the vault's filesystem. It SHALL refuse before copying, with the measured bytes and the free space, when the copy plus a reserve does not fit. The limits the adoption engine applies after staging SHALL be warnings, not refusals. Otherwise it SHALL copy each selected regular file into `_Imports/<import_id>/<folder name>/`, streaming, record a manifest of relative path, size and SHA-256, re-hash every copy, and report progress while copying. Every entry left behind SHALL get a per-path code in the manifest. The command SHALL never modify the original folder, and the folder's name SHALL be the collection label. A file that changed while it was read, could not be read, or was refused by the disk SHALL be left out with a code and SHALL keep the import open: the command SHALL exit with an error and say that the same command run again retries only those files under the same import id. A file that the vault refuses because of its own name or path SHALL be left out with the vault's code and SHALL NOT keep the import open: the import SHALL finish, and the report SHALL give the count of such files. An interrupted or incomplete copy SHALL resume from its manifest under the same import id without copying a verified file again. On a resume, a staged file that the folder no longer selects SHALL be removed and recorded in the manifest, and a staged file that changed after it was verified SHALL be copied again. The command SHALL name an unfinished import made with other options and SHALL NOT resume it, and SHALL NOT let two runs write the same import at once.

When the import finishes, the command SHALL end by reporting the bytes it staged, stating that the staged folder is a second copy of the data on the disk until it is discarded, naming the discard route that exists, and printing the `adoption_studio start` path. An import that stays open SHALL also print that path, as the way to adopt what is staged so far.

#### Scenario: A local folder is staged and adopted

- **WHEN** `evermatter import ~/Notes` runs for a folder outside the vault and adoption then starts on the printed path
- **THEN** the staged files' hashes equal the originals', the manifest accounts for every entry, and the collection label is the folder's name

#### Scenario: Hidden entries, ignored files and links stay behind and are reported

- **WHEN** the folder holds a hidden folder, a file that its `.gitignore` names, and a symbolic link
- **THEN** none of them is copied, the report before the copy counts each under its reason with bytes, and the manifest gives each a code

#### Scenario: A user opts back in

- **WHEN** the user passes `--include-hidden` or `--no-gitignore`
- **THEN** the entries of that reason are staged like the others, and the printed `start` path asks adoption to include hidden files when hidden entries were staged

#### Scenario: A file that cannot be copied keeps the import open

- **WHEN** a selected file changes while the command copies it
- **THEN** the file is not staged torn, the manifest gives it a code, the command exits with an error and names the rerun, and the same command run again copies only that file under the same import id

#### Scenario: A file the vault refuses does not hold the import open

- **WHEN** a selected file has a name that the vault never accepts
- **THEN** the manifest records the file as left out with the vault's code, the import finishes, the report counts the file, and the command prints the `start` path

#### Scenario: A file the disk refuses keeps the import open

- **WHEN** the disk refuses to store a selected file and the other files are staged
- **THEN** the manifest records the file as left out with a code, the command exits with an error and names the rerun, and the same command run again copies only that file under the same import id

#### Scenario: A resume removes what the folder no longer holds

- **WHEN** a file staged by an interrupted run is deleted from the folder before the rerun
- **THEN** the rerun removes the staged file, and the manifest records the removal

#### Scenario: A folder too large for the disk is refused before copying

- **WHEN** the folder's bytes exceed the vault filesystem's free space
- **THEN** the command refuses with the measured bytes and the free space, and copies nothing

#### Scenario: The command names the second copy

- **WHEN** staging finishes
- **THEN** the report gives the staged bytes, says that they duplicate the original until discarded, and names the discard route

#### Scenario: An outside path given to start is refused with the route

- **WHEN** `adoption_studio start` receives the absolute path of a folder outside the vault
- **THEN** it refuses with `IMPORT_PATH_OUTSIDE_VAULT` and names `evermatter import`
