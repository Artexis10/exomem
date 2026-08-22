## Why

Deferred media-sidecar commits currently pull graph floor and checkpoint files into the same atomic batch. On Windows, an ordinary reader holding `.graph-sync.json` can therefore turn a completed extraction into a partial three-file commit, failed rollback, and terminal `BATCH_ROLLBACK_INCOMPLETE` job. Status then tells the operator to repair or replace a healthy media artifact. This couples canonical evidence to rebuildable index state and makes routine file sharing look like data corruption.

## What Changes

- Add an explicit deferred-graph-completion mode for media commits: atomically stage the graph floor before caller-supplied canonical writes, but do not stage or replace the shared graph checkpoint.
- Keep ordinary inline graph transactions unchanged, including floor → caller writes → checkpoint ordering and fail-closed rollback.
- Let the existing post-commit fanout/deferred-index machinery complete the checkpoint and derived graph convergence after a media sidecar commits; the installed floor keeps the graph recovery-required across a crash gap.
- Classify legacy ambiguous batch failures as reconciliation-required infrastructure failures, never as corrupt media and never as safe blind retries.
- Add cross-platform and native Windows regression coverage proving a held graph checkpoint cannot roll back or terminalize completed extraction.

## Capabilities

### New Capabilities

- `transactional-vault-writes`: define a recoverable floor → canonical boundary for deferred graph completion while preserving every existing `post_commit_fanout` contract.
- `media-processing-reliability`: keep completed extraction committed when derived graph/index fanout fails, recover the freshness gap without re-extraction, and report legacy ambiguous failures truthfully.

### Modified Capabilities

None.

## Impact

- Affected code: atomic batch planning, media job failure classification/reconciliation, and existing deferred index fanout.
- Affected tests: transactional writes, media worker/jobs/processing, deferred convergence, and a native Windows sharing probe.
- Operations: existing `BATCH_ROLLBACK_INCOMPLETE` rows become reconciliation-required and will not be blindly retried; healthy binaries are not labeled corrupt.
- Data/dependencies: no schema migration, dependency, or lockfile change.
