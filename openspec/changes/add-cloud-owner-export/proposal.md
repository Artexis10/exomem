## Why

An Exomem Cloud owner has no self-serve way to take their memory out. Today an export needs an operator, and a suspended or ended account cannot get one at all. Owners must be able to download their notes and files as the canonical portable archive in every account state, without an operator handling the plaintext.

Home's storage bar also needs the real disk use. Every backup Job already measures it, but cellctl only uses the measurement to grow local volumes and never stores it.

## What Changes

- **Owner export, cell side.** A new `exomem cell-export` command builds the canonical Markdown-and-files ZIP with the quiesced vault export in owner scope. It uploads the ZIP to part URLs that cellctl presigned and reports only sizes, digests and fixed error codes.
- **Owner export, controller side.** cellctl runs the new contract C6 (`exomem_cloud_exports`):
  - it takes an `export` hold, which stops a running cell and keeps a stopped cell stopped;
  - it opens a multipart upload at `cells/<cell_id>/exports/<export_id>.zip` with the cell's own prefix-restricted key and runs the cell's export Job;
  - it checks the uploaded parts against the Job's report before it completes the upload;
  - it keeps a ready export for `EXOMEM_EXPORT_TTL_HOURS` (default 24) and gives a 300-second, version-pinned download link on request;
  - it deletes every object version at expiry, at supersede and when the cell is deleted.
- **Measured disk use.** cellctl records the filesystem's used and total bytes on the cell row from every backup and export Job report.
- **Settings.** The export TTL and the export deadline (default 30 minutes per started 10 GiB of the cell) are cellctl settings with chart values.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `cloud-cell`: adds owner export under an export hold, the verified export upload, export retention and links, and measured disk use. The capability's other requirements are still in the active change `adopt-exomem-cloud-plain-cells`, so this change only adds requirements.

## Impact

- **Exomem (`src/exomem`):** a new `cloud_export.py` module and the `exomem cell-export` command.
- **cellctl:** a new `exports.py` module, and changes to the decision function, the reconcile loop, the Job renderer, the Kubernetes client, the database layer and the settings. The schema fixtures gain C6, the `export` hold kind and the usage columns.
- **Platform chart:** `cellctl.exportTtlHours` and `cellctl.exportDeadlineMinutes`.
- **Substrate (its own change, launch-exomem-cloud-web):** migration 0066 with C6, the usage columns and the hold kind; the grants; the export API; Home; email 08; the privacy page.
- **Privacy:** a plaintext ZIP stays at the object store with provider encryption for at most the TTL, under the cell's own prefix. The web app never holds the cell's key or the bytes.
