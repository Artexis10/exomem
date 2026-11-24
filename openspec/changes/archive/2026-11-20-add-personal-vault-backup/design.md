# Design

## Context

See `proposal.md` for the outcome. These parts already exist:

- **Admission digest.** Every original's binding records `binary_sha256` and `binary_size`. Fixity already trusts it as the expected hash.
- **State registry.** `reserved_paths` is the closed authority for state names. `state_paths` places them under a per-vault state root outside the vault. Each family has a placement. `external-canonical` marks the families that a reset must never touch, but no field says which external families can be rebuilt.
- **A thread pattern.** `original_fixity.start` runs one daemon thread that the service starts and stops. `media_collect._restic` already wraps the `restic` executable with a timeout for the Cloud backup Job.
- **Cloud backup.** The Cloud image pins restic 0.19.1 by SHA-256 in its `Dockerfile`, and `cellctl` runs the backup Jobs. That path stays as it is.
- **The mutation guard.** A foreground write queues behind it. A background job that held it for minutes would turn into busy refusals.

The repository records no load target. This design sizes for one vault of 500,000 originals and 25 TB, and reports the missing target as debt.

## Goals / Non-Goals

**Goals:**

- An owner can point the service at a restic repository and have dated snapshots without outside scripts.
- A snapshot holds everything that cannot be rebuilt, taken consistently.
- The product proves a restore, against admission hashes, and shows the proof.
- Every failure is visible where the owner already looks.

**Non-Goals:**

- Exomem Cloud. It keeps its `cellctl` restic Jobs.
- Creating the repository without the owner's explicit flag.
- Backing up derived indexes. They rebuild.
- An MCP tool, a scheduler outside the service, or alerts outside `exomem status`.
- Replica custody for originals. A separate change owns it.
- Reading a B2, S3 or SFTP account. restic owns every backend.

## Decisions

### D1: Personal installs only. The vault is the artefact and originals are included by default

The backup is for installs that run the service on the owner's machine. A Cloud cell sets no backup config and runs no thread.

Originals are part of the vault, so they are in the snapshot by default. An owner can set `include_originals` to false when replicas elsewhere cover them. The backup then leaves out each bound original and the original pool. It passes restic the list of paths it keeps (a folder with no original below it is one entry), because one exclude pattern per original costs restic a comparison per file and pattern: 49 to 74 s at 64,000 files with 31,846 originals, against 2 to 26 s for the list (the slowest run read a cold file cache). A snapshot of such a backup names every entry it holds as a path, so the vault root is a tag on the snapshot and retention groups by host only. The status warns that originals are out, and shows how many. It shows how many of them lack a recoverable replica as `unknown`. The replica-custody change will supply that number. This change reads nothing from it.

Rejected alternative: exclude originals only when replicas exist. That needs the replica-custody change, which has not shipped. An opt-out the owner states is simple and honest.

### D2: One owner config file outside the vault

The file is named by `EXOMEM_VAULT_BACKUP_CONFIG`. It follows the principle of `EXOMEM_MEDIA_STORE_CONFIG`: a vault copy must not carry the means to read the backup. A path to the vault, the password file or the environment file that lies inside the vault makes the entry invalid, so a stray copy of the vault carries none of them.

Each vault has one entry, keyed by its resolved path. The keys are:

| Key | Default | Meaning |
|---|---|---|
| `repository` | required | A restic repository string. Passed to restic as it is. |
| `password_file` | required | Path to the repository password file. |
| `environment_file` | none | Path to `NAME=value` lines for backend credentials. |
| `interval_hours` | 24 | Time between backups. |
| `keep_last`, `keep_daily`, `keep_weekly`, `keep_monthly` | 0, 7, 4, 12 | Keep rules. At least one must be above zero. |
| `check_interval_days` | 7 | Time between repository checks. |
| `check_subset_parts` | 12 | A check reads part `n` of this many, and `n` rotates. |
| `sample_interval_days` | 7 | Time between sampled restores. |
| `sample_size` | 20 | Originals, and notes, in each sample. |
| `sample_max_mib` | 64 | Largest original that a sample restores. |
| `include_originals` | true | Whether original bytes join the snapshot. |

The code does not parse the repository string beyond "a non-empty string", and never branches on its scheme. A repository that is a local path inside the vault is refused, because the snapshot would contain itself. That check compares two paths and reads no scheme.

Secrets are only file paths. The service reads the environment file once per restic call and puts its values in that subprocess's environment. It sets `RESTIC_REPOSITORY` and `RESTIC_PASSWORD_FILE` in that environment too, so a repository string that embeds a credential never shows in a process list. It drops any `RESTIC_*` variable that the service's own environment carries. It never stores or logs a secret. Status shows a short fingerprint of the repository string, never the string. A restic message that reaches a log first has every configured secret value and the repository string replaced.

Rejected alternative: add the keys to `EXOMEM_MEDIA_STORE_CONFIG`. `cellctl` writes that file for Cloud cells, and a second owner for it would risk both.

### D3: A service thread, one flight, every call bounded

`vault_backup.start` runs one daemon thread, started and stopped like `original_fixity`. A stop ends a running restic process within seconds, and a step that a stop cut short is not recorded as a failure. It starts only in the serving runtime, because a stdio session is too short to own a long backup. The thread starts when `EXOMEM_VAULT_BACKUP_CONFIG` is set and reads the file again at every cycle, so an edit needs no restart. With the variable unset, no thread starts.

A file lock under the vault's state directory makes a cycle single-flight, across the service thread and the CLI. A second runner reports "already running" and does nothing.

Each restic call has a timeout and runs with `subprocess.run` and an argument list. Only calls that are safe to repeat retry, at most twice with a pause: `snapshots`, `check` and `forget`, and a `restore` that a repository lock stopped before it wrote. A `restore` that timed out never runs again into the same target: `--overwrite never` skips the file that the first run left truncated and reports success. `backup` never retries inside a cycle, and the next cycle repeats it. The backup timeout is 7 days, because restic resumes by reusing uploaded data and a first backup of a very large vault has to finish inside one call. The other calls have shorter named limits.

The thread never takes the vault mutation guard. restic reads the vault as plain files, as a file-sync agent does. The in-process walks, which build the exclude list and the sample, run under `foreground_priority` and yield to requests. A backup, a check and a restore therefore cause no busy refusal.

`restic` must be on `PATH` at version 0.17.1 or later, the first release with distinct exit codes for a missing repository (10) and a wrong password (12). When it is absent or older, the status says "restic not found" or "restic too old" and no thread work runs. Nothing crashes.

The thread never creates a repository. A missing repository reads as `repository_missing` in status. The owner creates it with `exomem backup run --init-repository`, an explicit choice. Auto-creation would back up into a typo, or into an unmounted mount point.

Reuse check. `resticprofile` and `autorestic` schedule restic from outside the process. They cannot quiesce Exomem state, compare restored bytes with admission hashes, or report in `exomem status`. That is the gap. The added maintenance cost is one scheduler thread, one subprocess wrapper, and one CI install step for restic.

### D4: Non-rebuildable state is declared once, in the registry, and staged with SQLite's backup API

`reserved_paths.InternalStateDescriptor` gains one closed field, `rebuildable`, default true. A family sets it to false when its files hold information that no vault file, index or recomputation can restore. The three `external-canonical` families set it false, and the registry refuses a canonical family that does not. These external families also set it false: the review state, the voice profiles, the ownership-review ledger, and the media jobs store. The jobs store keeps authoritative offload protections, as its own module states.

The existing registry had no such declaration. `external-canonical` says a reset must not touch a family. It does not cover the families that a reset may rebuild but that hold owner decisions. A second attribute is the smallest change that names them. The default is true so a new family never joins a backup by accident. A test pins that every `external-canonical` family declares false.

The backup stage walks the state root's top level, classifies each name through `reserved_paths.classify_logical`, and keeps a family only when it is external and not rebuildable. It holds no list of file names or patterns of its own.

For each kept file:

- A SQLite database goes through `sqlite3.Connection.backup`, in one step so the copy reads one snapshot, into a staging directory under the state root. The source opens read-write with `query_only`: a read-only open leaves a `-wal` and a `-shm` file beside a database that was closed cleanly. The copy is then checked with `PRAGMA quick_check`. A failed check is a finding, and the copy is still staged.
- `-wal`, `-shm` and `-journal` files are never copied. The suffix set is the SQLite family that `reserved_paths` already defines.
- Any other regular file is copied whole. These files are replaced atomically, or are append-only.
- Symbolic links are skipped.

restic then backs up the vault root and the staging directory in one snapshot. The staging directory is removed after the call.

A stage failure does not stop the backup of the vault. It makes the backup `partial` and a finding names the code. A partial backup never triggers retention, so a snapshot with a hole cannot push out an older complete one. The next backup waits the normal interval, because a file that stays unreadable would otherwise add a snapshot every hour and nothing would prune them.

Rejected alternative: a filename glob list in the backup module. It goes stale on the day a family is added and cannot say why a file matters. Rejected alternative: copy live SQLite files raw. A torn copy restores as a corrupt database.

### D5: Verification reads the repository and proves a restore

**Check.** On the check interval, `restic check --read-data-subset=n/N` runs, with `N` from `check_subset_parts`. `n` advances by one after each success and wraps, so the whole repository is read over `N` checks, about a quarter at the defaults. A failed check is recorded as a finding, and the next check reads the same part again, so the finding stays until that part passes. Only a timeout or a held repository lock is retried within a call.

**Sampled restore.** On its interval, the service restores a sample from the latest snapshot into a fresh temporary directory under the state root, then deletes it.

1. It draws `sample_size` originals and `sample_size` notes with reservoir sampling over the live bindings and notes. Memory stays bounded at any vault size. It skips evicted originals, originals above `sample_max_mib`, and files whose inode change time is later than the snapshot's own start time, so a file edited or moved since the snapshot is never drawn.
2. It restores each chosen file and its sidecar with `restic restore --verify`. restic checks every restored file against the snapshot's recorded content, so a note is proved byte-identical to the snapshot and not to the live file. If the batch restore fails, the service restores each path alone to name the failing one.
3. It hashes each restored original and compares it with the `binary_sha256` of the restored sidecar, the binding as the snapshot recorded it. A change since the snapshot therefore causes no false alarm.
4. A file the snapshot lacks, or a hash that differs, is a finding with the vault-relative path and a code.

With `include_originals` false, only notes are sampled, and the status says originals are not part of the proof.

Rejected alternative: compare restored files with the live files. A file edited an hour after the snapshot would read as a backup failure.

### D6: Retention follows a complete backup

After a complete backup, `restic forget --prune` with the entry's `--keep-last`, `--keep-daily`, `--keep-weekly` and `--keep-monthly` counts runs, restricted to the snapshots that carry this change's tag and the vault's root tag and that this machine took (`--host`), as one group. A snapshot of another vault, machine or tool in the same repository is not touched. Restic's default grouping by host and paths would apply one vault's keep rules to every other vault in the repository, and a backup without originals records a different path list each time. A machine that is renamed starts a new group; its older snapshots stay until the owner forgets them. A partial or failed backup never runs it.

### D7: Owner operations are a CLI, and restore writes only to a separate directory

`exomem backup` has three subcommands:

- `status` prints the status block.
- `run` runs one cycle now, and `--init-repository` creates a missing repository first.
- `restore` restores paths from a snapshot into a target directory.

Each shell command is local to the machine, so the person who runs it is the owner, as for `exomem status` and `exomem doctor`. Each operation also calls `egress.owner_only_aggregate`, which passes for a call that no request surface bound, so a later binding to a surface stays owner-only.

`restore` refuses a target that is inside the vault, that holds the vault, or that overlaps the vault's state directory. It requires the target to be new or empty, and passes `--overwrite never` to restic. A restore never writes in place. Paths must be relative, without `..`, and restic's glob characters are escaped, so a name is never read as a pattern. `--state` restores the staged state instead of vault paths. The operation reports the snapshot, the target and the file count.

Rejected alternative: an MCP tool. It would need a public-surface entry, schemas and a principal, for an operation that writes bytes anywhere on the host's disk. The CLI is the smallest surface that serves a local owner. A later change can bind the same functions to a surface.

### D8: Status comes from a small JSON record that can be lost

The service records the last attempt of each activity in one JSON file under the vault's state directory: backup, retention, check, sampled restore and findings. It is written whole through a rename. A missing or unreadable record reads as `unknown`. An activity not yet run in a readable record reads as `never`. Neither is zero.

`vault_backup.status(vault)` is cheap: it reads the config and the record and probes for restic. `resource_status.collect` adds it as `vault_backup`, and `exomem status` prints it. The block carries `state`, the repository fingerprint, `include_originals`, the last backup (time, snapshot ID, files, bytes, outcome), the last retention, the last check, the last sampled restore (counts and failures), the next due time of each activity, and findings with paths and codes. The block is for the owner's local tools and may name vault paths.

Rejected alternative: a SQLite table. The record is a few hundred bytes and rebuilds from the next cycle, so a database would add a schema for nothing.

## Risks / Trade-offs

- **Cross-file consistency.** restic reads live files, so a note and its sidecar can be captured at different instants. The next backup closes the gap. The sampled restore compares each original with the sidecar from the same snapshot.
- **Staging space.** Staging copies the non-rebuildable SQLite state once. A full disk makes the backup `partial` and shows a finding.
- **First backup on a very large vault.** At 25 TB the first call can run for days. The 7-day timeout is a bound, and a killed call resumes by reusing uploaded data.
- **Check cost.** At the defaults a weekly check reads about one twelfth of the repository. At 25 TB, a remote backend charges that egress. The owner tunes `check_subset_parts` and `check_interval_days`.
- **Sample selection by change time.** A file that changed after the snapshot is not drawn. A file that arrived after the snapshot with an older change time, for example a copy that keeps its timestamps, can read as missing from the snapshot. The next sample, after a newer backup, clears it.
- **Derived rows in a canonical family.** The `collection-store` descriptor also names `collections-derived.sqlite`, which is rebuildable. Staging copies it with the store because the registry declares the family as one unit. Splitting the descriptor is old debt, and it would only save space.
- **The repository shares a failure domain.** A repository on the same disk is no protection against disk loss. The status cannot know. The owner chooses.
- **Prune cost.** `forget --prune` on a very large repository can run for hours, inside its timeout, after a backup and before the next due check.

## Migration Plan

Nothing migrates. With no config the feature is off, and `exomem status` reads "not configured".

The registry field adds no migration: its default leaves every other family as it was.

Rollback is a release downgrade. The record and staging directory are private state that an older release ignores.

An entry with a key the service does not know is invalid, so a misspelled keep rule is never replaced by a default that prunes history. The registry's `rebuildable` attribute has no default for an external family: a new family must choose. Existing snapshots stay in the owner's repository.

## Open Questions

None block this change. The count of excluded originals without a recoverable replica needs the replica-custody change, and shows `unknown` until it ships.
