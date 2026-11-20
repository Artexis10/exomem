# Proposal

This change gives the owner of a personal Exomem install a backup that the service runs, verifies and reports. The owner points the service at a restic repository. The service keeps dated snapshots of the vault and of the machine-local state that nothing can rebuild. It checks the repository, proves a sample restores to the recorded admission hashes, and shows all of this in status.

## Why

A personal vault lives on one machine. Exomem Cloud already backs up its cells with its own restic Jobs. A personal install has no product backup, so each owner builds one from outside tools.

Those tools fail in the same three ways:

- **They copy the wrong state.** The vault is a folder, but the state that cannot be rebuilt sits in the machine-local state root, in SQLite files and small files that change while they are copied. A copy that picks files by name goes stale when a new file appears. A raw copy of a live SQLite file can be torn.
- **They never prove a restore.** A repository that has not been read back is a hope. The vault already records an admission SHA-256 for every original, so the product can prove a restored original is the one that was admitted. No outside tool knows that.
- **They report nowhere the owner looks.** A failed nightly job is silent until the day the owner needs it.

## What Changes

- A new owner config file, outside the vault, names one restic repository per vault. It holds paths to secrets, never secrets.
- A service thread runs a backup on an interval. One snapshot holds the vault and a staged copy of the non-rebuildable state. SQLite files go through SQLite's online backup API. Raw write-ahead-log and journal files are never copied.
- The state registry gains one closed attribute per state family: whether the family is rebuildable. The backup stages exactly the families that declare they are not.
- After a complete backup, the service applies the owner's keep rules with `restic forget --prune`.
- On an interval, the service runs `restic check` over a rotating part of the repository. On another interval, it restores a sample of originals and notes from the latest snapshot into a temporary directory. Each restored original must hash to its admission SHA-256.
- Three owner operations ship as `exomem backup`: `run`, `status` and `restore`. `restore` writes only into a new, separate target directory.
- `exomem status` gains a `vault_backup` block with the last backup, last check, last sampled restore, the next due times and any findings. Unknown and never are visible states, never zero.
- The owner can leave original bytes out of the backup. The status then warns, and reports `unknown` for the count of excluded originals that no replica covers, until the replica-custody change supplies that count.

This change adds no cloud scheduler, no repository creation without the owner's explicit flag, no MCP tool and no change to Exomem Cloud's backup.

## Capabilities

### New Capabilities

- `personal-vault-backup`: Owner configuration, scheduled snapshots, retention, repository check, sampled restore proof, owner operations and status.

### Modified Capabilities

- `machine-local-state-placement`: Each reserved state family declares whether it is rebuildable.

## Impact

The change adds one module, `vault_backup`. It edits the state registry by one field, the service runtime by one start and stop hook, `exomem status`, and the CLI table by one command. It adds a runtime dependency on the `restic` executable, only for an owner who configures a backup, and no Python dependency. A new CI step installs the pinned restic release that the Cloud image already pins. Nothing changes for an install that sets no backup config.
