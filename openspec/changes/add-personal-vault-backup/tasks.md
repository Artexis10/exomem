# Tasks

## 1. State declaration

- [ ] 1.1 Add the closed `rebuildable` attribute to the state registry, set it false for the three `external-canonical` families and the four owner-decision families named in the design, and fail registry load for a canonical family that is rebuildable. Prove that the registry exposes exactly those families as not rebuildable, and that a rebuildable canonical family is refused.

## 2. Configuration and restic wrapper

- [ ] 2.1 Add `vault_backup` with config loading from `EXOMEM_VAULT_BACKUP_CONFIG`, secret handling in the subprocess environment only, and the bounded `restic` wrapper. Prove that an entry inside the vault is invalid, that secrets are absent from the argument list and present in the environment, and that a log line scrubs configured values.

## 3. Snapshot, retention and staging

- [ ] 3.1 Stage non-rebuildable state with SQLite's online backup API and run `restic backup` and `restic forget --prune`. Prove with a real restic binary that a database written during staging restores and passes its integrity check, that no write-ahead-log file is staged, and that a failed backup forgets nothing.
- [ ] 3.2 Support `include_originals` false. Prove that the snapshot holds no original's bytes and that status warns with the unreplicated count as `unknown`.

## 4. Verification

- [ ] 4.1 Add the rotating `restic check` and the sampled restore against the admission hashes. Prove with a real restic binary that a healthy repository passes, and that a corrupted pack surfaces as a finding with a path or code.

## 5. Owner operations and status

- [ ] 5.1 Add `exomem backup status`, `run` and `restore`, with the restore refusals. Prove that a deleted note restores byte-identical from an earlier snapshot into a new directory, and that a target inside the vault refuses.
- [ ] 5.2 Add the `vault_backup` block to `exomem status` and the service start and stop hook. Prove "not configured", "restic not found", unknown before the first run, and a lost record reading as unknown.

## 6. Acceptance

- [ ] 6.1 Run the end-to-end workflow on a disposable vault and a disposable local repository with a real restic binary: configure, back up, forget, check, sampled restore, delete a note, back up again, restore the note from the first snapshot into a target directory, and read each step in the status. Record the log.
- [ ] 6.2 Add the pinned restic install to the CI job that runs these tests, and make a missing restic fail in CI and skip with a clear reason locally.
