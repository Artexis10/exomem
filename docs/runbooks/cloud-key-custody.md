<!-- authority:non-specification -->

# Exomem Cloud key custody

Each cell reads and writes its backups with one object-store key. The key custodian creates
that key. `cellctl` holds no credential that creates or deletes keys.

## Clean up after a cell deletion

`cellctl` cannot delete the deleted cell's key. When a cell reaches `deleted`, it raises the
platform alert `cell-storage-cleanup-pending-<cell_id>`. Delete the key as follows.

1. Record the cell ID and the cell's deletion time from the alert and the control database.
2. Choose the account. A cell with a stored B2 key ID belongs to `founder`; a newer cell belongs to `cell-data`.
3. Export the operator key for that account. It needs `listKeys` and `deleteKeys`.
   For `cell-data` it also needs `listFiles` and `writeFiles`.
   `export CELL_CLEANUP_B2_KEY_ID=<key id> CELL_CLEANUP_B2_APPLICATION_KEY=<application key>`
4. Run the dry run. It changes nothing and prints counts only.
   `infra/scripts/cell_storage_cleanup.py <cell_id> --account founder`
5. If `keys_found=1`, run the same command with `--apply`. Expect `keys_deleted=1`.
6. For `cell-data`, add the bucket ID and the deletion time to both commands.
   `infra/scripts/cell_storage_cleanup.py <cell_id> --account cell-data --bucket-id <locked bucket id> --deleted-at <ISO 8601 time with offset>`
7. Expect `hide=waiting` before the 7-day grace ends. Rerun with `--apply` after the date it prints.
   Expect `hide=due` and `files_hidden` equal to `files_found`.

The script deletes only keys that the provider lists with the exact prefix `cells/<cell_id>/`.
It hides the cell's current backup files and never deletes a file version: the bucket's
compliance lock keeps every version until its retention ends. Every step is safe to repeat.
If `keys_found` is more than 1, stop and ask why the provider holds two keys for one cell.
