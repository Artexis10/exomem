# Spec Delta

## Purpose

Keep dated, verified restic snapshots of a personal vault and its non-rebuildable machine-local state, prove that a sample restores, and report every step to the owner.

## ADDED Requirements

### Requirement: Backup is off until the owner configures it, and secrets stay out of the vault

The service SHALL run no backup work until an owner config file, named by `EXOMEM_VAULT_BACKUP_CONFIG`, holds a valid entry for the vault. The status SHALL read "not configured" when the variable is unset or the file has no entry for the vault.

An entry SHALL name a restic repository string, a password file path and optionally an environment file path. The service SHALL pass the repository string to restic without parsing it beyond requiring a non-empty string. An entry whose password file, environment file or local repository path lies inside the vault SHALL be invalid, and the status SHALL name the invalid field. A config file that lies inside the vault SHALL be invalid, and the status SHALL name `EXOMEM_VAULT_BACKUP_CONFIG`. An entry that holds a key the service does not know SHALL be invalid, and the status SHALL name that key, because a misspelled keep rule would otherwise fall back to a default that prunes history.

The service SHALL pass the repository, the password file path and every backend credential to restic only in the subprocess environment. It SHALL NOT store a secret, SHALL NOT show the repository string in status, and SHALL replace a configured secret value in any log line.

#### Scenario: No config

- **WHEN** `EXOMEM_VAULT_BACKUP_CONFIG` is unset
- **THEN** no backup thread starts and no restic process runs
- **AND** the status reads "not configured"

#### Scenario: A credential file inside the vault

- **WHEN** an entry's password file lies inside the vault
- **THEN** the entry is invalid, no backup runs, and the status names the password file field

#### Scenario: A misspelled key

- **WHEN** an entry holds `keep_montly` and no other keep rule
- **THEN** the entry is invalid, no backup runs, and the status names `keep_montly`

#### Scenario: A config file inside the vault

- **WHEN** `EXOMEM_VAULT_BACKUP_CONFIG` names a file inside the vault
- **THEN** the entry is invalid, no backup runs, and the status names `EXOMEM_VAULT_BACKUP_CONFIG`

#### Scenario: Secrets never reach the command line

- **WHEN** the service runs a restic call for a configured entry
- **THEN** the repository string and every credential value are absent from the process arguments
- **AND** they are present in the subprocess environment

### Requirement: A snapshot holds the vault and the non-rebuildable state, taken consistently

A backup SHALL write one restic snapshot that holds the vault root and a staged copy of every machine-local state family that declares itself not rebuildable. It SHALL copy each SQLite database with SQLite's online backup API in one step, so the copy reads one snapshot of the database, through a connection that refuses writes. It SHALL NOT copy a write-ahead-log, shared-memory or journal file, and it SHALL NOT leave one beside a database that was closed cleanly. A stop request SHALL end staging between files. It SHALL exclude every rebuildable family. It SHALL NOT hold the vault mutation guard while it runs.

When staging a state file fails, the backup SHALL still snapshot the vault, SHALL record the outcome `partial` with the failing code, and SHALL NOT apply retention.

#### Scenario: A live SQLite database

- **WHEN** a non-rebuildable SQLite database is being written during a backup
- **THEN** the snapshot holds a copy that SQLite opens and that passes its quick integrity check
- **AND** the snapshot holds no `-wal`, `-shm` or `-journal` file of it

#### Scenario: Rebuildable indexes stay out

- **WHEN** the state root holds embedding, lexical and graph indexes
- **THEN** the snapshot holds none of them

#### Scenario: Foreground writes continue

- **WHEN** a backup runs and a client writes a note
- **THEN** the write completes without a busy refusal caused by the backup

### Requirement: The original bytes are included unless the owner opts out

By default the snapshot SHALL hold every original's bytes. When the entry sets `include_originals` to false, the backup SHALL leave out each bound original and the original pool. The work to leave them out SHALL grow with the number of files plus the number of originals, never with their product. The status SHALL then warn that originals are excluded, SHALL show how many, and SHALL show the count of excluded originals without a recoverable replica as `unknown` until a source for that count exists.

#### Scenario: Opt-out

- **WHEN** the entry sets `include_originals` to false
- **THEN** the snapshot holds the notes and sidecars and no original's bytes
- **AND** the status warns and reports the unreplicated count as `unknown`

### Requirement: Retention follows a complete backup

After a backup whose outcome is complete, the service SHALL run `restic forget --prune` with the entry's keep rules, restricted to the snapshots of this vault that this machine took, and treating them as one group. It SHALL NOT remove a snapshot of another vault, of another machine, or of another tool, which may share the repository. It SHALL NOT run retention after a partial or failed backup. A successful retention SHALL record when it last succeeded.

#### Scenario: Keep rules apply

- **WHEN** a complete backup succeeds and older snapshots exceed the keep rules
- **THEN** the older snapshots are forgotten and their data pruned

#### Scenario: A shared repository

- **WHEN** two vaults with different keep rules back up to one repository, or two machines back up the same vault path
- **THEN** each retention applies its own rules to its own snapshots and removes no other

#### Scenario: A failed backup keeps history

- **WHEN** a backup fails
- **THEN** no snapshot is forgotten

### Requirement: The service checks the repository and proves a sampled restore

On the check interval, the service SHALL run `restic check --read-data-subset=n/N`. `n` SHALL advance after each successful check and wrap at `N`.

On the sample interval, the service SHALL restore a sample of originals and notes from the latest snapshot into a temporary directory and delete it afterwards. It SHALL restore with restic's own verification, so each restored note is proved identical to the snapshot's recorded content. It SHALL hash each restored original and compare the digest with the `binary_sha256` of the sidecar restored from the same snapshot. A damaged restore, a missing file or a digest mismatch SHALL be a finding with the failing vault-relative path and a code. A sample that draws no file SHALL read `empty`, never as a passed restore.

#### Scenario: A healthy repository

- **WHEN** a snapshot holds originals with bindings
- **THEN** the sampled restore reports each sampled original as verified against its admission SHA-256
- **AND** reports no finding

#### Scenario: A damaged pack

- **WHEN** a data pack in the repository has altered bytes
- **THEN** a check or a sampled restore that reads it reports a finding
- **AND** the status shows the failed outcome and its code

#### Scenario: A rotating check

- **WHEN** a check succeeds on part 3 of 12
- **THEN** the next check reads part 4

### Requirement: An owner restores a path only into a separate target directory

The owner operation `exomem backup restore` SHALL restore selected paths, or the staged state, from a named snapshot into a target directory. It SHALL refuse a target that is inside the vault, holds the vault or overlaps the vault's state directory. It SHALL refuse a target that exists and is not empty. It SHALL NOT write into the vault in any case. It SHALL prefer this machine's latest snapshot of the vault, then another machine's of the same vault path, and SHALL refuse a choice that is ambiguous between several. It SHALL refuse an absolute path, a path with `..`, and SHALL treat glob characters in a path literally.

The owner operations SHALL be owner-only.

#### Scenario: Recover a deleted note

- **WHEN** the owner deletes a note, a later backup runs, and the owner restores that note from the earlier snapshot into a new directory
- **THEN** the restored file is byte-identical to the note at the earlier snapshot
- **AND** the vault is unchanged by the restore

#### Scenario: A target inside the vault

- **WHEN** the owner names a target directory inside the vault
- **THEN** the operation refuses and writes nothing

### Requirement: Status reports every step, and unknown is never zero

The status SHALL report the last backup (time, snapshot ID, files, bytes and outcome), the last check, the last sampled restore (counts and failures), the next due time of each, and the findings. A value the service cannot read SHALL be `unknown`, which includes the findings and the last error when the record is lost. An activity that has not run SHALL be `never`. Neither SHALL read as zero. A sampled restore that drew nothing SHALL read `empty` and SHALL NOT record a success. The status SHALL show the keep rules in effect. When restic is absent the status SHALL read "restic not found", and when it is older than the supported version, "restic too old". A missing repository SHALL read `repository_missing`.

The service SHALL record this state in a small private file under the vault's state directory, and a missing or unreadable file SHALL read as unknown.

#### Scenario: Restic is absent

- **WHEN** a valid entry exists and `restic` is not on `PATH`
- **THEN** the status reads "restic not found" and the service raises no error

#### Scenario: Before the first run

- **WHEN** the entry is valid and no cycle has run
- **THEN** the last backup, check and sampled restore show `unknown`, not zero or an empty list

#### Scenario: A lost record

- **WHEN** the record file is deleted after backups ran
- **THEN** the status shows each last-run value as `unknown`
- **AND** the next cycle writes the record again

### Requirement: Only one backup flight runs, and every restic call is bounded

At most one cycle SHALL run per vault across the service thread and the CLI. Every restic call SHALL have a timeout. Only calls that are safe to repeat SHALL retry, with a bounded count and a pause. A restore that timed out SHALL NOT run again into the same target, because restic would skip a file that it left truncated and report success. A repository lock, which fails before restic writes anything, MAY be retried.

#### Scenario: A second runner

- **WHEN** a cycle runs and the owner starts `exomem backup run`
- **THEN** the second runner reports "already running" and runs no restic call
