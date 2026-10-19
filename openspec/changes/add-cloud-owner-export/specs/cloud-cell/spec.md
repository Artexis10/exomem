## ADDED Requirements

### Requirement: An owner export runs under an export hold

cellctl SHALL pack an owner export only while the cell carries an `export` hold and no runtime pod uses the cell's volume. The hold SHALL stop a running or read-only cell and SHALL keep a stopped cell at zero replicas. cellctl SHALL record the hold on the row with its start time and SHALL resume it after a restart. An export SHALL wait while another hold owns the volume. An export SHALL NOT start while a relocation or planned move of the cell's volume is due, or while a cell that has had a volume has no claim; cellctl SHALL release a leftover export hold during that wait.

The hold SHALL have a deadline that grows with the export's cap: the configured deadline for each started 10 GiB. The export Job and its part URLs SHALL NOT outlast it. Past it, the export SHALL fail with `EXPORT_HOLD_EXPIRED`. When the export ends, cellctl SHALL release the hold only after the export Job and its pods are gone, and the cell SHALL return to its desired replicas. When the cell is deleted, the export SHALL fail with `EXPORT_INTERRUPTED` and SHALL NOT delay the deletion. cellctl SHALL retry a failed abort of its upload on every pass while the cell has a key.

#### Scenario: A suspended account exports

- **WHEN** the owner of a stopped cell requests an export
- **THEN** the export becomes ready and the cell's runtime never starts

#### Scenario: A running account exports

- **WHEN** the owner of a running cell requests an export
- **THEN** the runtime stops, the export Job starts only after the runtime's pod has released the volume, and the runtime starts again after the Job is gone

#### Scenario: A cell due to move exports

- **WHEN** the owner requests an export while a relocation or planned move of the cell's volume is due
- **THEN** the move runs first, and the export then packs the volume the cell serves from

#### Scenario: A cell whose claim is gone has an export

- **WHEN** a cell that has had a volume has no claim while its export is requested, packing or holding the cell
- **THEN** cellctl applies nothing for the export, the cell reports `VOLUME_MISSING`, and the export waits for the volume

#### Scenario: An export Job hangs

- **WHEN** an export hold passes its deadline
- **THEN** the export fails with `EXPORT_HOLD_EXPIRED` and the cell returns to its desired replicas

### Requirement: An export is uploaded only as the parts the cell reported

The cell's export Job SHALL build the canonical archive with the quiesced vault export in owner scope and SHALL upload it only to part URLs that cellctl presigned over HTTPS for the export's own object, `cells/<cell_id>/exports/<export_id>.zip`. The Job MUST NOT hold an object-storage credential. Its report SHALL carry only sizes, digests, the filesystem's use and fixed error codes. The Job SHALL accept a vault as large as the cell can hold, and SHALL report `EXPORT_TOO_LARGE` only for a file above the per-file limit.

cellctl SHALL create the upload with the cell's own prefix-restricted key and with encryption at rest. It SHALL complete the upload only when the listed parts match the Job's report in number, ETag digest and total size. It SHALL record the completed object version.

#### Scenario: A cell grown past 10 GiB

- **WHEN** an owner exports a cell whose size is 110 GiB
- **THEN** the Job may pack up to 110 GiB, and at most 1000 presigned parts cover the archive

#### Scenario: A part is replaced before completion

- **WHEN** a part is overwritten through its part URL after the Job reported it
- **THEN** cellctl does not complete the upload, the export fails with `EXPORT_UPLOAD_FAILED`, and the upload is removed

#### Scenario: Another cell's key or object

- **WHEN** cellctl creates, completes, links or deletes an export
- **THEN** it names only the object that export's own row derives, and it signs only with that row's cell key

### Requirement: A ready export is kept for the TTL and served only through short version-pinned links

A ready export SHALL expire `EXOMEM_EXPORT_TTL_HOURS` after it became ready (default 24). For each link request cellctl SHALL issue one download link that lasts 300 seconds, names the verified object version and asks for an attachment. At expiry, at supersede and when the cell is deleted, cellctl SHALL clear the link, delete every version of the object and then record the export as expired. A failed deletion SHALL be retried and SHALL NOT leave a link in place.

#### Scenario: The owner asks for a link

- **WHEN** the owner requests a link for a ready export
- **THEN** cellctl records one link that serves the verified version until it expires, and the store refuses it after 300 seconds

#### Scenario: The export expires

- **WHEN** a ready export passes its expiry
- **THEN** no version of its object remains, its link fields are empty, and it is recorded as expired

### Requirement: The cell row records the disk use its Jobs measure

cellctl SHALL record the filesystem's used and total bytes, and the time of the measurement, on the cell row from every backup and export Job report that measured them. It SHALL write the three values together and only when `0 <= used <= total` and `total > 0`. Until a Job has measured the cell, all three values SHALL stay empty, so that a reader can show the use as unknown.

#### Scenario: A backup measures the volume

- **WHEN** a backup Job reports its filesystem's used and total bytes
- **THEN** the cell row records them, as of the backup

#### Scenario: A cell was never measured

- **WHEN** no backup or export Job has reported a measurement for a cell
- **THEN** the row's used bytes, filesystem bytes and observation time are all empty
