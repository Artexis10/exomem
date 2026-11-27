## Why

The restore drill (`infra/scripts/cloud_restore.sh drill CELL SNAPSHOT REFERENCE`) restores a cell's snapshot into scratch, then compares both backed-up roots with a reference. The reference is the live cell, or the roots that an earlier in-place restore kept. The comparison is the only end-to-end check that a backup holds the cell's data. It requires every file to match, so a serving cell can never pass it.

On 2026-11-27 a drill restored a 02:05Z snapshot of a production cell. The vault root matched the live cell: 11,709 of 11,709 files were identical. The host root did not: the live cell had 760 files that the snapshot lacked, and 10 files differed. The cell had restarted on a new release 2 seconds after the snapshot. Since then it had written lock files and counters and rewritten its SQLite indexes. The drill reported `verdict=fail`. The files it found were runtime state that the cell wrote after the restart. That is an inference from their directories, because the run captured no change times.

The runbook already says that a live mismatch "does not alone prove that the backup or restore is defective". The verdict line does not make that distinction, so a run that proved the vault restores reads as a failed drill.

## What Changes

- The drill judges only what the reference has not changed since the backup read it. Each reference entry whose change time (ctime) is earlier than the snapshot's time must be in the restore with the same content. A later change, and a restored entry that the reference no longer has, is reported as divergence and does not fail the drill.
- The cutoff is the snapshot's own time, from restic's snapshot record. Every backup of a cell that the drill supports reads the stopped cell's volume, so nothing changes between the stop and restic's start.
- The rule applies to both references: `live`, and `prior:<id>`, whose roots hold the cell as an in-place restore stopped it.
- The log prints, per root, how many entries the rule judged and how many it reported. A drill whose reference vault holds entries but whose rule judged none of them ends `verdict=inconclusive`, not `pass`.
- A comparison that cannot run fails the drill, apart from divergence.
- The verdict logic moves from the shell script into `cloud_vault_digest.py`, so tests can drive it on fixture trees.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `cloud-platform-operations`: adds one requirement on what a restore drill judges and how it reports divergence.

## Impact

- `infra/scripts/cloud_vault_digest.py`: `hash --ctime` for the reference capture; a `judge` command applies the rule.
- `infra/scripts/cloud_restore_manifests.py`: prints the backup's exclude patterns for `judge`, as `backup-paths` prints the paths.
- `infra/scripts/cloud_restore.sh`: drill cutoff, verdict lines and the `inconclusive` verdict. The in-place restore's hashing and comparison do not change.
- `docs/runbooks/cloud-operator-restore.md` and `tests/test_cloud_restore_tools.py`.
- cellctl's backup and restore Jobs do not change.
