## Context

- A Cloud cell registers only MCP and health routes and has no egress. cellctl reaches only the Kubernetes API, the control database and object storage. The web app holds no cell key, no object-storage key and no Kubernetes access.
- Import (contract C5, `add-exomem-cloud-vault-import`) already runs cell Jobs under a hold that stops the runtime, uploads through presigned multipart parts, and deletes every object version when it ends.
- `vault_portability.export_quiesced_vault` builds the canonical archive. It verifies the archive with `verify_export_archive` before it publishes it.
- Every backup Job already writes the filesystem's used and total bytes into its termination message.

## Goals / Non-Goals

**Goals:**
- An owner exports in every account state: running, read-only, stopped (suspended or ended).
- No operator-controlled component other than the object store holds the archive's bytes, and the store holds them for at most the TTL.
- Home shows the real disk use, or an unknown state, never an invented value.

**Non-Goals:**
- The HTTP API, the email and the Home page. Substrate owns them.
- An export of a running cell without a pause. See D1.

## Decisions

### D1. An `export` hold stops the cell for the whole export

cellctl takes an `export` hold, exactly as for an import: it records the hold on the row, scales the runtime to zero, and waits until no runtime pod uses the volume. A stopped cell is already at zero replicas, and the hold keeps it there. The Job runs only then. The hold ends when the export Job and its pods are gone. The cell then returns to its desired replicas.

The hold has a deadline: `CELLCTL_EXPORT_DEADLINE_MINUTES` (default 30) for each started 10 GiB of the export's cap (D2). A 10 GiB cell gets 30 minutes, and a 110 GiB cell gets 330. The Job's `activeDeadlineSeconds` and the part URLs' expiry use the same deadline. Past it, the export fails with `EXPORT_HOLD_EXPIRED` and the cell starts again. A hold that no export row owns ends after twice the deadline, when no export Job can still run; an orphaned import hold ends the same way after twice the import deadline. An export waits while another hold (upgrade, backup, snapshot backup, restore, import or move) owns the volume. It also does not start while a relocation or planned move of the volume is due, or while the cell has had a volume and its claim is missing; routine reconciliation owns the volume then, and a leftover hold from an earlier export is released meanwhile. An export therefore packs the volume the cell serves from. An import that claims the volume first goes first.

*Rejected for now: a snapshot clone on local storage.* The shared contract asks for a clone that keeps the cell serving. A crash-consistent clone of a running cell can hold a collection store ahead of its published replica, and the quiesced export refuses that (`COLLECTION_STORE_FLUSH_PENDING`). So clone exports would fail intermittently for owners who write collection rows. Local storage has not shipped, and the product accepts a few-minute pause until it does. The stopping hold works on every storage class.

### D2. The cell's Job uploads to presigned parts and holds no key

cellctl opens the multipart upload with the cell's own prefix-restricted key and `ServerSideEncryption=AES256`. An export may pack everything the cell can hold: the cell's size, or its measured filesystem when that is larger. Storage add-ons grow a cell past 10 GiB, so the portability module's 10 GiB default does not apply. cellctl passes this cap to the Job as `--max-bytes`. It presigns parts that cover the cap plus 2% for the ZIP's own headers: 64 MiB each, or larger in whole MiB so that at most 1000 parts cover it. The URLs expire at the export deadline.

The Job runs `exomem cell-export` on the cell image. It mounts the cell's volume, stages the archive on an emptyDir as large as its parts, and uploads the parts in order. Its environment carries no object-storage credential.

The Job mounts the volume read-write, like the import Jobs. SQLite cannot read a WAL-mode database on a read-only mount once its `-shm` file is gone, and the export reads the collection store. The hold keeps the runtime stopped, so no other writer exists.

### D3. cellctl completes only the parts the Job reported

The Job reports `{bytes, sha256, parts, etag_digest, used, total}`. Before completion cellctl lists the upload's parts and checks:

- the part numbers are exactly 1 to `parts`;
- the ETag digest of the listed parts equals `etag_digest`;
- the listed part sizes add up to `bytes`.

After completion it checks the completed version's size again. A substituted part fails with `EXPORT_UPLOAD_FAILED`, and cellctl aborts the upload. A missing upload is never accepted, because it cannot be checked again.

### D4. A ready export is kept for the TTL and served only by short links

`expires_at` is `ready_at` plus `EXOMEM_EXPORT_TTL_HOURS` (default 24). On a link request cellctl presigns a GET for 300 seconds. The GET names the version it verified and asks for an attachment. cellctl answers each request once: `link_issued_at` is never earlier than the request it answers.

At expiry, at supersede and when the cell is deleted, cellctl first clears the link, then deletes every version and delete marker of the object, and then records `expired`. If the deletion fails, the export stays `ready` without a link, and cellctl tries again on the next pass. On a deleted cell cellctl records `expired` anyway, and the prefix deletion in D5 removes the versions.

### D5. Failure records the outcome first, then removes the upload

On a failure, at the deadline or on cell deletion, cellctl records the C6 outcome, deletes the Job, and then removes the upload with the cell's own key. The row keeps `upload_id` until the abort succeeds, and every pass retries it while the cell has a key. A failed removal never holds the cell.

On a deleted cell the final mechanism is the deleted-cell prefix deletion. cellctl lists every file version under `cells/<id>/` with the controller key, deletes each one, and deletes the cell's key only when the listing is empty. The bucket has no lifecycle rule for unfinished large files.

Backblaze's API documentation (read on 2026-10-12) says that this deletion also removes an unfinished upload:

- [`b2_list_file_versions`](https://www.backblaze.com/apidocs/b2-list-file-versions) returns unfinished large files with the action `start` ("a large file has been started, but not finished or canceled"). It needs `listFiles`.
- [`b2_delete_file_version`](https://www.backblaze.com/apidocs/b2-delete-file-version), used on an unfinished large file, "has the same effect as `b2_cancel_large_file`". It needs `deleteFiles`.

The controller key has both capabilities. Task 4.2's live check on B2 is the proof.

### D6. Disk use is an observed column group on C1

cellctl writes `used_bytes`, `filesystem_bytes` and `usage_observed_at` together from every backup Job report (as of the backup) and from every export Job report (as of the Job's completion). A report whose numbers do not add up (`0 <= used <= total`, `total > 0`) is not written. Until the first report the three columns are null, and Home shows the use as unknown.

## Shared contract C6

The Substrate migration 0066 is the schema of record. cellctl's tests apply `infra/cellctl/tests/fixtures/exomem_cloud_exports.sql`, a copy of its Cloud part.

**C1 additions**
- `hold_kind` also allows `export`.
- `used_bytes bigint`, `filesystem_bytes bigint`, `usage_observed_at timestamptz`: all null or all set, with `filesystem_bytes > 0` and `0 <= used_bytes <= filesystem_bytes`. Written by cellctl only.

**`exomem_cloud_exports`**
- Substrate writes `export_id uuid primary key`, `cell_id` (references C1, `ON DELETE RESTRICT`), `requested_state` (`requested` or `superseded`; a supersede is final) and `link_requested_at`.
- cellctl writes `observed_state` (null, `packing`, `ready`, `failed`, `expired`), `error_code`, `archive_bytes`, `archive_sha256`, `archive_version_id`, `upload_id`, `ready_at`, `expires_at`, `download_url`, `download_url_expires_at` and `link_issued_at`.
- The database writes `created_at` and `updated_at`. `export_id`, `cell_id` and `created_at` are immutable.
- A partial unique index allows one export per cell with `observed_state` null or `packing`.
- An insert, or a change to `requested_state` or `link_requested_at`, calls `pg_notify('exomem_cloud_cells', cell_id)`. cellctl's own writes do not.
- `archive_version_id` is an addition to the frozen contract: a link can pin the verified version only if cellctl stores it.

**Privileges**

| Role | C6 | C1 usage columns |
|---|---|---|
| `substrate_app` | select; insert `export_id`, `cell_id`, `requested_state`, `link_requested_at`; update `requested_state`, `link_requested_at`; no delete | select |
| `exomem_cellctl` | select; update the cellctl columns above | select; update |
| `exomem_gateway` | none | none |

**Error codes:** `EXPORT_FAILED`, `EXPORT_TOO_LARGE`, `EXPORT_HOLD_EXPIRED`, `EXPORT_INTERRUPTED`, `EXPORT_UPLOAD_FAILED`, `SOURCE_CHANGED_DURING_EXPORT`. The Job may report `EXPORT_FAILED`, `EXPORT_TOO_LARGE`, `EXPORT_UPLOAD_FAILED` and `SOURCE_CHANGED_DURING_EXPORT`. cellctl records any other report as `EXPORT_FAILED`. The Job reports `EXPORT_TOO_LARGE` only for a file above the portability module's per-file limit (2 GiB). Any other exceeded limit, and an archive that outgrows its parts, is `EXPORT_FAILED`.

**Job report:** the termination message is `{"bytes", "sha256", "parts", "etag_digest", "used", "total"}` on success. On failure it is `{"error"}`, plus `used` and `total` when the Job measured them, so that C1 keeps the newest measurement.

**ETag digest:** SHA-256 of one line `"<part number> <ETag>\n"` per part, in part order, with the ETag's surrounding double quotes removed. Shared test vector: the ETags `"0cc175b9c0f1b6a831c399e269772661"` and `"92eb5ffee6ae2fec3ad71c777531578f"` give `91124627b9b8d6bffcbbfb35d09b5fb3cea67f87e4e84d341fac87ba0d0586da`. Both the cell command's tests and cellctl's tests assert it.

## Risks / Trade-offs

- **A running cell pauses for the export.** Hetzner volumes cannot snapshot, and D1 rejects the clone for now. Home says so before the owner starts an export.
- **Part URLs appear in the Job's arguments.** An identity that can read Jobs in the cell namespace can upload a part. D3 rejects any part the Job did not report, so this can cause a failed export, not a substituted one.
- **A plaintext archive sits at the object store for up to the TTL.** It has provider encryption at rest, under the cell's own prefix. Deletion is verified per version. The privacy page says this.
- **An upload whose abort fails until a deleted cell's key is gone** depends on the prefix deletion to remove it. Nothing else removes it: the bucket has no lifecycle rule. Task 4.2 checks this on B2 before the export API goes on.

## Migration Plan

1. Substrate applies migration 0066 and the grants with the export API off.
2. The cell image with `exomem cell-export` rolls out, owner canary first.
3. cellctl rolls out. An export on an older image fails with `EXPORT_FAILED`, because the command is missing.
4. Substrate turns the export API on after the rehearsal.

Rollback: turn the export API off. C6 rows stay, and cellctl still expires and deletes ready exports.

## Open Questions

- Should the clone-based export return once local storage ships and the collection store can publish its replica on demand?
