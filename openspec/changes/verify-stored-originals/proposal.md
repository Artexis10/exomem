## Why

Exomem records a SHA-256 for every stored original, but nothing reads the original again after the write. A corrupted, edited or deleted original therefore goes unnoticed until someone opens it, and by then a clean copy may be gone. Originals are the evidence that every compiled note rests on, so their loss is silent damage to the vault.

## What Changes

- **A background fixity thread.** Exomem starts its own daemon thread for fixity, as it starts the graph drain. It does not depend on the media worker, which `EXOMEM_DISABLE_MEDIA_EXTRACTION` removes.
- **Presence first, then a budgeted re-hash.** Each pass stats every known original. It hashes a changed file at once and reports a missing file. Then it re-hashes originals, oldest verification first, within a byte budget and a read-rate cap.
- **Restore only from a matching copy.** On a mismatch, fixity restores from a copy whose SHA-256 matches the binding. It moves the changed bytes to Trash and never adopts the new hash. With no matching copy it changes nothing and reports `corrupt_no_copy`.
- **Owner and operator signals.** The owner sees an attention item and an `originals` block in `exomem status`. A Cloud cell adds a content-free count to readiness, and cellctl alerts on it.
- **Its own state.** Fixity keeps identity, last verification and state in its own table. It finds originals through walks that already exist, never through a derived lexical index. Text originals fenced in a Source page are verified and reported, never repaired, because a repair would write the page.
- **On by default, soft fail.** Every install runs the thread, because an integrity check that is off protects nothing. `EXOMEM_DISABLE_ORIGINAL_FIXITY=1` stops it for a rollback without a release revert. A start or pass failure never denies the core service.

Agents see no new tool and no new step. No server-side model is added. Duplicate detection is a separate change, and this change neither specifies nor depends on it.

## Capabilities

### New Capabilities

- `original-fixity`: background verification of stored originals, restore from a matching copy, and the owner and operator signals.

### Modified Capabilities

None. The attention, status and readiness surfaces gain one category, one block and one count, and no existing requirement changes.

## Impact

- **Exomem (`src/exomem`):**
  - a new fixity module with the thread, pass, budget, restore and table;
  - the thread start in `server_runtime.py`;
  - one attention category in `attention.py`;
  - the `originals` block in `resource_status.py`, and the readiness count in `runtime_readiness.py`.
- **cellctl:** an alert on the readiness count, read as the recall-lag alert in `cell_readiness.py` is read.
- **Runbook:** restoring one original from the restic backup, in `docs/runbooks/cloud-operator-restore.md`. The serving pod holds no backup keys, so this step stays manual.
- **Cost:** one budgeted reader per install. It never downloads an evicted Cloud original, so verification costs no egress.
- **Local installs** keep no backup copy of originals in this change. An opt-in mirror folder is a later change.
