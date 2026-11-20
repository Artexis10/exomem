# Personal vault backup

This guide is for the owner of a personal install. It does not cover Exomem Cloud, which keeps its own backup Jobs.

The service keeps dated [restic](https://restic.net) snapshots of your vault and of the machine-local state that nothing can rebuild (review decisions, voice profiles, the collection store and its archive, the media jobs store and the procedure approval key). It never copies an index or a cache, because the product rebuilds them.

The service checks the repository, restores a sample to prove that the backup works, applies your keep rules, and shows each result in `exomem status`. It does nothing until you name a config file.

## Set it up

1. Install restic 0.17.1 or later and put it on the `PATH` of the service.
2. Write the repository password to a file outside the vault. Allow only your own account to read it. Keep a second copy away from this machine: nobody can read the repository without it.
3. Write a config file outside the vault (example below). Allow only your own account to read it.
4. Set `EXOMEM_VAULT_BACKUP_CONFIG` to the config file in the service environment. Restart the service.
5. Run `exomem backup run --init-repository`. The command creates the repository and takes the first backup.
6. Run `exomem backup status`. Each line shows a state, and `findings` is empty.

The service never creates a repository by itself. A typo in `repository` then cannot start a new backup in the wrong place.

## The config file

The file is JSON. Each key under `vaults` is the absolute path of one vault.

```json
{
  "vaults": {
    "/path/to/vault": {
      "repository": "/mnt/backup/exomem-repository",
      "password_file": "~/.config/exomem/backup-password",
      "environment_file": "~/.config/exomem/backup-credentials",
      "include_originals": true,
      "interval_hours": 24,
      "keep_daily": 7,
      "keep_weekly": 4,
      "keep_monthly": 12
    }
  }
}
```

Only `repository` and `password_file` are required. A secret never appears in this file: `password_file` and `environment_file` are paths. The environment file holds `NAME=value` lines for the backend, such as the S3 key pair. The service passes them to restic only. A leading `~` in either path means the home directory of the user that runs the service.

| Key | Default | Meaning |
| --- | --- | --- |
| `include_originals` | `true` | Set `false` to leave original files out. Status then counts them and warns. |
| `interval_hours` | 24 | Time between backups. |
| `keep_last`, `keep_daily`, `keep_weekly`, `keep_monthly` | 0, 7, 4, 12 | restic keep rules. restic keeps the union of them. |
| `check_interval_days`, `check_subset_parts` | 7, 12 | Each check reads one part of N of the repository data, and the part advances each time. |
| `sample_interval_days`, `sample_size`, `sample_max_mib` | 7, 20, 64 | Each sample restores this many notes and originals, and skips an original larger than the limit. |

The service refuses a `password_file`, `environment_file` or local `repository` path inside the vault.

## Read the status

`exomem status` shows one `vault_backup` line. `exomem backup status` shows the last backup, retention, check and sampled restore, what is due next, and each finding with its path and code.

- `unknown` means the service cannot read the record. It never means zero.
- `never` means the activity has not run yet.
- `empty` means a sampled restore had nothing to draw, for example a vault with no notes. It never counts as a passed restore.
- `restic not found` means restic is not on the `PATH` of this process.

A damaged repository shows as a finding with `check` or `sample` as the activity. To start again, give `repository` a new location and run `exomem backup run --init-repository`.

## Recover files

The restore command never writes into the vault. It writes into a new or empty directory that you name.

1. Run `exomem backup restore --target /path/to/new-directory --path "Knowledge Base/Notes/example.md"`.
2. Add `--snapshot <id>` to restore from an older snapshot. The default is the latest.
3. Add `--state` instead of `--path` to restore the staged machine-local state.
4. Copy the files you need back into the vault yourself.

The command refuses a target inside the vault, a target that holds the vault, and a target that already holds files.

## What this does not protect against

A repository on the same disk as the vault does not protect you from losing that disk. Choose a repository on another device or service.

With `include_originals` set to `false`, an original with no other copy is not in this backup.
