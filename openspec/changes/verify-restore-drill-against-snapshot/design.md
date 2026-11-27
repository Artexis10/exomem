## Context

A cell's backup Job runs `restic backup` over `/data/vault` and `/data/host` (`BACKUP_PATHS` in `infra/cellctl/src/cellctl/manifests.py`), less `CATALOGUE_BACKUP_EXCLUDES`. The drill supports cells that are not on local storage (`cloud_restore_manifests.py` refuses local cells). For those cells, every backup reads the stopped cell's own volume: the nightly backup, the pre-upgrade backup and the move backup. Hourly backups and upgrade backups from a VolumeSnapshot clone exist only for local cells.

The drill restores a snapshot into a scratch namespace with cellctl's own restore Job. It hashes both restored roots and both reference roots with `cloud_vault_digest.py hash`, then requires an exact match through `compare`. A hash line holds an entry's digest and relative path. A symlink's digest covers its target, and the entry type is recorded. The in-place restore uses the same `hash` and `compare` while the cell is down.

`/data/host` holds derived state, custody records and runtime caches. On 2026-11-27, in the 7.5 hours after a snapshot, a serving cell wrote 759 lock files and one media storage record, and rewrote 10 files: counters, generation caches and the SQLite indexes.

restic already checks every blob it restores against the blob's SHA-256 and exits 1 on any error. cellctl's `restic check --read-data-subset` Jobs check the stored data on their own schedule. What no check covers is whether the snapshot holds what the cell held, and the drill's reference comparison exists for that.

The engineering roster's pick-by-problem table has no row for backup verification, so no roster source applies.

## Goals / Non-Goals

**Goals:**

- A drill passes on a serving cell whose backup holds what the cell held when the backup read it.
- A drill fails when an entry that existed unchanged at the backup is missing from the restore, or comes back different.
- The log says how much the drill judged, so a judgement over almost nothing is visible.

**Non-Goals:**

- Changing what a backup covers or when it runs, or any cellctl Job.
- Changing the in-place restore's hashing or comparison.
- Drilling local-storage cells. Their clone backups freeze at the VolumeSnapshot, before restic starts, so they would need that time as the cutoff.

## Decisions

### D1: Judge only what the reference has not changed since the backup

Each reference entry is compared with the restore when its ctime is earlier than the cutoff. Such an entry must exist in the restore with the same digest; otherwise the drill fails. A reference entry with a later ctime is reported as changed since the backup. A restored entry that the reference lacks is reported as removed since the backup: a deletion leaves no time to judge by. An entry that matches the backup's exclude patterns is reported, never judged.

ctime, not mtime: a write, a rename, a chmod or a new link all move ctime forward, and a program cannot set it back. An entry with an older ctime has not changed since that moment.

Alternatives considered:

- **Report all divergence and judge only the restore.** This gives up the only check that the backup holds the cell's data. A snapshot that held 1 of 11,709 vault files would pass.
- **Skip caches and lock files.** The SQLite indexes and custody records under `/data/host` also change on a serving cell, so this still fails, and every new runtime file needs a new rule.
- **Stop the cell to capture the reference.** That makes a drill cost downtime, and the drill promises never to stop the cell.
- **`restic restore --verify`.** It re-reads the bytes restic wrote, regular files only, mostly from the page cache. restic's normal restore already checks each blob's hash. It adds cost without the check that matters.

### D2: The cutoff is restic's snapshot time

The drill reads the snapshot's time from restic's snapshot record, in the scratch restore Job's log line that restic prints when it starts ("restoring snapshot … at …"). It reads the line right after the Job finishes, because the Job is deleted 300 s later. It truncates that time down to the second. A missing or unparsable line fails the drill. For a supported cell the backup read a stopped volume, so no entry can change between the stop and that time. A clock read or a query time would fall later and judge entries the cell wrote after the backup.

### D3: The drill says how much it judged

Per root, the log prints `judged=N changed_since=N removed_since=N excluded=N`. When the reference vault holds entries and the rule judged none, the verdict is `inconclusive` with a non-zero rc. That happens when the snapshot is older than every entry, or when every ctime moved, for example after a recursive ownership change (the cell pod sets `fsGroupChangePolicy: OnRootMismatch`). A host root may legitimately judge nothing.

### D4: The verdict lives in `cloud_vault_digest.py judge`

- `hash --ctime` adds each entry's ctime in nanoseconds to its line. Only the drill's reference capture uses it, so plain `hash` lines and `compare`, which the in-place restore uses during downtime, stay as they are.
- For a regular file, `hash --ctime` takes the ctime from `fstat` on the open file after reading it. A file the cell rewrites during the read then shows its new ctime and is reported, not judged against old content.
- `judge RESTORED REFERENCE CUTOFF_NS OUT_DIR --root ROOT --exclude PATTERN…` writes names to the private run directory and prints counts only. It exits 0 on pass, 3 on a judged mismatch, 4 on inconclusive, and 1 when it cannot run.
- `cloud_restore_manifests.py exclude-patterns` prints `CATALOGUE_BACKUP_EXCLUDES`, as `backup-paths` prints `BACKUP_PATHS`, so the list keeps one definition. `judge` keeps the patterns under its `--root`, strips that root, and matches the rest with restic's meaning, in which `*` does not cross `/`.
- `cloud_restore.sh` maps each exit code to the verdict, and any code it does not know fails the drill. Tests drive `judge` on fixture trees, as they already drive `compare`.

## Risks / Trade-offs

- **A clock difference between the backup Job's node and the cell's node could misjudge an entry changed close to the cutoff.** Mitigation: a judged mismatch is reported with its root and count, so the operator can check that entry's times by hand before treating the backup as defective.
- **restic does not restore a Unix socket.** A serving cell gives a socket it binds a fresh ctime, so the rule reports it rather than judging it.

## Migration Plan

Ship with an ordinary release. The next drill uses the new verdict. Rollback is the previous release: the drill copies its release files to its run directory when it starts.
