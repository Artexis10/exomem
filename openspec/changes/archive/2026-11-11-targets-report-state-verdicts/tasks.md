# Tasks

## 1. Target-reported verdict

- [x] 1.1 Add `vault_state_verdict(vault)` to `exomem.service_manager`: the
  `_state_migration_required` decision computed under the calling release's own
  catalogs, plus an `unrecognized_compatibility_ids` refusal, returning
  `{"verdict": "serve"|"migrate"|"refuse", "reason": str}`.
- [x] 1.2 Extend the `WorkerRuntime.inspect` probe to run
  `vault_state_verdict` under the target interpreter and report the dict as
  `state_verdict`; parse it into the verified tuple, treating an absent or
  malformed report as `None` so old targets degrade.
- [x] 1.3 Rework `WorkerRuntime._state_migration_required` to consume the
  reported verdict (refuse → `ValueError`, migrate → `(True, reason)`), keeping
  the launcher-side read as the fallback when the target cannot report, and
  returning `(True, "state manifest unreadable by supervisor")` when neither
  side can read the state instead of raising.

## 2. Tests

- [x] 2.1 A manifest recording an optional format id the supervisor's catalogs
  do not know: first start inspects the active target, takes its verdict, and
  spawns the worker; no `StateMigrationManifestError` reaches `start()`.
- [x] 2.2 The same manifest with a candidate that reports `refuse`: the
  upgrade refuses with the target's reason and the serving worker is untouched.
- [x] 2.3 A target whose probe cannot report falls back to the launcher-side
  computation (both accept and refuse outcomes).
- [x] 2.4 Neither side can read the state: handoff declares the migration;
  first start proceeds to spawn the active worker.

## 3. Docs and spec

- [x] 3.1 Update `docs/managed-service-upgrades.md` "A supervisor older than
  the vault's state": the supervisor no longer needs the format catalog to
  start the recorded worker; `upgrade_blocker` still gates orchestration.
- [x] 3.2 `openspec validate --all --strict` clean before and after any
  archive.
