# Relocate machine-local state out of the vault

## Why

Exomem keeps every piece of machine-local derived state — the graph epoch
and floor JSON (`.graph-sync*.json`), commit receipts, coordination locks,
the deferred-index, embeddings, lexical, clip, refs, media-jobs and
governance stores, rebuild scratch files, due/review state — inside the
vault directory it indexes. The vault is user content, and users sync user
content: Syncthing, Dropbox, OneDrive, iCloud.

Measured on 2026-08-27 (personal cell, Syncthing folder "Knowledge Base"):

- The sync agent's hasher re-scanned a live rebuild scratch
  (`.lexical.sqlite.rebuild-<id>.tmp-wal` — a name that evades every
  `.stignore` pattern) every ~60 seconds, holding Windows file handles that
  collide with the service's atomic finalize. The warm-up phase retried in
  a loop and the cell could not reach steady serving for hours.
- `.graph-sync.json` (the graph epoch), `.graph-sync-floor.json`, the
  recovery marker, `.graph-commit-receipts/` and `.governance.sqlite` were
  syncing between three devices. A foreign epoch file landing under a live
  service classifies as `GRAPH_SYNC_LINEAGE_CONFLICT` and demands a
  whole-vault rebuild — the shape of the unexplained 15:47 rebuild chain
  that produced a 3,232-row full-demand backlog and a day-long outage.
- The `.stignore` in that vault already carries a comment documenting a
  *previous* incident of the same class (a synced hot journal triggering
  SQLite rollback recovery on a peer). Blocklisting file names loses this
  race every time a new state file is invented.

As a concrete local-first precedent, Basic Memory at audited commit
`ea38fd76` resolves its application state per user (`resolve_data_dir()` →
`BASIC_MEMORY_CONFIG_DIR`, else `$XDG_CONFIG_HOME/basic-memory`, else
`~/.basic-memory`). Its configured project holds the human-owned Markdown,
while `memory.db`, watcher status, model cache and sync bookkeeping stay in the
application-state directory. This is a factual comparison, not a code
dependency. The defect is not the graph design; it is Exomem's failure to
preserve the same content/state boundary as operational features accumulated.

## What changes

- Persistent vault-scoped machine-local state moves to a per-user, per-vault
  state root outside the vault: an absolute `EXOMEM_STATE_ROOT` override, else
  the platform state dir (`%LOCALAPPDATA%\exomem\state` on Windows,
  `$XDG_STATE_HOME/exomem/state` or `~/.local/state/exomem/state` on POSIX),
  keyed by a stable vault identity.
- `reserved_paths` remains the closed authority for reserved names and gains
  an explicit placement classification: vault-canonical, external-state, or
  target-adjacent. A single resolver becomes the only source of paths for the
  external-state class. Batch and held-publication intermediates remain beside
  their destination because same-volume atomic publication depends on it; they
  are bounded transaction scratch, not persistent state stores.
- One-time migration runs only through explicit
  `exomem maintain --migrate-state --offline` authority after every legacy
  writer is proven stopped. Ordinary service and stateful CLI startup is a
  read-only readiness gate: it never creates, copies, resumes, upgrades, or
  deletes state and returns stable `STATE_MIGRATION_OFFLINE_REQUIRED` until
  the transition is complete. Ambiguity and any later legacy duplicate refuse
  rather than selecting an authority.
- Vault content is unaffected: notes, `_access.yaml`, and every
  human-owned or syncable artifact stay in the vault. Regenerable external
  state rebuilds from vault content. Portable-derived state, including review
  decisions and graph receipts, remains explicitly included in export/restore
  so moving its runtime copy does not silently weaken portability.

## Impact

- Affected specs: new `machine-local-state-placement` capability; touches
  the operating assumptions of `live-index-freshness` and
  `hosted-vault-portability` without changing their requirements.
- Affected code: `reserved_paths.py` (name and placement authority), every
  external-state constructor, read-only startup gate, explicit offline
  migrator, hosted binding and restore, portability, deployment, and
  doctor/health placement surfaces.
- Cells: Windows and POSIX desktop deployment stop and prove the selected old
  worker and listener gone, persist one operator/service state-root binding,
  install the target, migrate offline, run doctor, start, and prove the new
  worker/version. Hosted rollout closes and drains routes, proves zero runtime
  pods, and uses a target-image migration Job holding the hosted lifetime lock
  plus the migration lock. Hosted restore itself relocates portable-derived
  state under that lifetime lock before a candidate can become ready, even
  when the deployment migration mode is `none`. The only rollback exception is
  an offline governance v4-to-v3 break-glass protocol. It first publishes the
  exact pre-terminal v3 image (`D0`) at the predecessor path, then commits and
  proves the sole terminal receipt-head transition (`D1`), aligns that legacy
  image to `D1`, and advances the schema fence last. An immutable rollback
  marker makes relocated admission refuse while the predecessor runs. A later
  descriptor-scoped offline adoption re-externalizes governance only, preserving
  all unrelated external families and any predecessor write. The existing
  `EXOMEM_WRITER_LEASE_STATE_DIR` contract stays separate and unchanged.
