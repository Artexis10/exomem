## ADDED Requirements

### Requirement: cellctl publishes the import limits it enforces

cellctl SHALL write `exomem_cloud_cells.import_limits` for each live cell. It SHALL publish an object for attachable storage and NULL for local storage. The object SHALL hold the keys that the C5 contract names, and each key SHALL come from one source: the value cellctl passes to the import Job, a cellctl setting, or the cell's size cap. cellctl SHALL write the column only when the object or NULL differs from the stored value, and a write SHALL NOT change the cell's generation.

`max_storage_gib` SHALL be at least the cell's current size, `max(storage_gib, grown_storage_gib)`. On a cell on attachable storage, it SHALL be the larger of the configured cap and the current size. Local import growth remains unfinished. Cellctl SHALL classify storage from the actual PVC class, then the PV class, then the configured domain. It SHALL actively clear an earlier limits object on a local cell. Local cells SHALL keep legacy admission, which does not raise `storage_gib` or `grown_storage_gib`.

#### Scenario: The operator changes the cap

- **WHEN** the operator raises the configured cap and cellctl next reads a live cell on attachable storage
- **THEN** cellctl writes an object with the new `max_storage_gib`
- **AND** the cell's generation does not change

#### Scenario: Nothing changed

- **WHEN** the computed object equals the stored `import_limits`
- **THEN** cellctl writes nothing

#### Scenario: A paid cell is larger than the cap

- **WHEN** a cell's current size is 30 GiB and the cap is 20 GiB
- **THEN** its published `max_storage_gib` is 30

#### Scenario: A local cell has an earlier limits object

- **WHEN** cellctl reads a local claim with an earlier published limits object
- **THEN** it writes NULL and changes no generation
- **AND** import admission keeps both storage sizes unchanged

#### Scenario: A local claim has not bound a PV

- **WHEN** the actual PVC class is local and no PV is bound
- **THEN** cellctl publishes NULL even when the configured domain is attachable storage

#### Scenario: A new local claim

- **WHEN** no PVC or PV supplies a class and the configured domain is local
- **THEN** cellctl publishes NULL

### Requirement: One configured cap bounds cell growth for every storage class

cellctl SHALL read one size cap, `max_cell_gib`, from the top level of its cell-storage setting. The cap SHALL bound a local cell's growth after a backup and an import's growth alike. A fleet with no local storage SHALL have the cap too. cellctl SHALL accept the earlier key inside the `local` object, and SHALL refuse to start when both keys are set with different values.

#### Scenario: A Hetzner-only fleet

- **WHEN** no local storage is configured
- **THEN** the cap is the configured value, or its default, and the published `max_storage_gib` uses it

#### Scenario: A chart that still sets the earlier key

- **WHEN** only the earlier `local` key is set
- **THEN** cellctl uses that value as the cap

#### Scenario: Two caps disagree

- **WHEN** the top-level key and the earlier `local` key are both set to different values
- **THEN** cellctl refuses to start and names both keys

### Requirement: An import that needs growth waits for the volume before it holds the cell

While an import has a `required_storage_gib` and no recipient, cellctl SHALL take no hold and render no prepare Job. This lasts until three things hold: the cell's observed capacity covers the requirement, `observed_storage_generation` equals the cell's generation, and the storage receipt shows no resize pending. The runtime pod SHALL stay mounted during the wait. A restart of the pod that the raise causes SHALL count as waiting. The wait SHALL read capacity and the claim's conditions, not pod readiness alone.

The wait SHALL end at `import_growth_deadline`, counted from the import's creation. Its default SHALL be 60 minutes, above `backup_deadline`, and the operator SHALL be able to set it. On expiry, cellctl SHALL fail the import with `IMPORT_STORAGE_UNAVAILABLE` through the existing cleanup.

Local import growth SHALL remain unshipped until an authoritative node budget covers physical allocations, accepted pending requests and same-pass increases. That budget SHALL include unclaimed physical volumes supported by recovery. Publication, admission and every local allocation increase SHALL use that budget when local growth ships. Cached free bytes, a same-pass counter or Kubernetes inventory alone SHALL NOT establish this guarantee. Existing local backup growth SHALL keep its earlier behavior in this delivery.

Before local import growth ships, cellctl SHALL retain each accepted claim request when its increase cannot fit beside the snapshot reserve. This SHALL cover cleanup and later reconciliation after failure. Desired size SHALL stay raised, and cellctl SHALL NOT shrink a claim. Unknown room SHALL leave growth waiting; known insufficient room SHALL fail with `IMPORT_STORAGE_NO_ROOM`. The local cutover rehearsal SHALL prove this behavior before local import growth ships.

#### Scenario: The claim has not grown

- **WHEN** an import has a requirement above the observed capacity
- **THEN** the cell has no hold and no prepare Job exists
- **AND** the import row has no recipient

#### Scenario: The raise restarts the pod

- **WHEN** the cell pod restarts while the claim expands
- **THEN** the import stays pending and does not fail

#### Scenario: The claim never grows

- **WHEN** the deadline passes with the requirement unmet
- **THEN** the import ends `failed` with `IMPORT_STORAGE_UNAVAILABLE` and no recipient was written

#### Scenario: The volume has grown

- **WHEN** the observed capacity covers the requirement and no resize is pending
- **THEN** cellctl takes the hold and renders the prepare Job
- **AND** the import reaches `key_ready`

#### Scenario: A future local growth cannot fit

- **WHEN** local import growth is enabled and its authoritative node budget can no longer hold the required size at the check before the key
- **THEN** the import ends `failed` with `IMPORT_STORAGE_NO_ROOM` and no key exists
- **AND** the claim keeps its last accepted request while desired `storage_gib` stays raised
- **AND** cleanup and later reconciliation request no larger claim until the node can hold it

### Requirement: The import Job enforces the limits that cellctl publishes

cellctl SHALL pass the member limit and the free-space reserve to the import Job as `EXOMEM_IMPORT_MAX_MEMBERS` and `EXOMEM_IMPORT_RESERVE_BYTES`. The Job SHALL read them, and SHALL use its defaults when they are absent. Published limits objects SHALL use those same two values.

#### Scenario: The Job limit is one member lower

- **WHEN** an archive holds one member more than `EXOMEM_IMPORT_MAX_MEMBERS`
- **THEN** the Job refuses it with `ARCHIVE_TOO_LARGE` and the vault is unchanged

#### Scenario: An image that predates the variables

- **WHEN** the Job runs on an image that ignores the variables
- **THEN** it enforces its own defaults, which equal the published defaults

### Requirement: An import records the files and bytes it staged

When an import Job succeeds with a valid report, cellctl SHALL write `staged_files` and `staged_bytes` together before cleanup can remove the Pod report. Both values SHALL fit the non-negative PostgreSQL `bigint` range, excluding booleans. A missing or invalid report SHALL leave the counts unknown and SHALL NOT block cleanup. A later failure, cancellation or expiry MAY retain the raw immutable pair. Public replies SHALL expose the pair only for `done` with no pending cancel or discard intent.

#### Scenario: An import succeeds

- **WHEN** the Job reports 12 files and 3,400 bytes and the import reaches `done`
- **THEN** the import row carries `staged_files` 12 and `staged_bytes` 3400

#### Scenario: An import fails

- **WHEN** the Job fails without a successful report
- **THEN** the row's staged columns stay NULL

#### Scenario: Cleanup restarts after a successful staging report

- **WHEN** cellctl restarts after it records a valid report and before cleanup ends
- **THEN** the immutable pair survives Job deletion and finalization
- **AND** public replies hide the pair if a later cancellation or failure ends the import

### Requirement: The CLI checks the published limits before it creates an import

Before it creates an import, the CLI SHALL read the `limits` that the import listing carries. With an object, the CLI SHALL use `max_members` and `reserve_bytes` for its local checks. With no `limits` key, the CLI SHALL use its built-in defaults. With `limits: null`, the CLI SHALL say that limits are not published and SHALL apply no member or space limit.

The CLI SHALL map `IMPORT_TOO_MANY_FILES`, `IMPORT_STORAGE_LIMIT` and `IMPORT_PARTS_LIMIT` to errors that name the phase of the refused call and do not offer a resume. It SHALL print the typed `limit` and no file name. After `IMPORT_PARTS_LIMIT` the import is open, so the CLI SHALL say to cancel it and start a new run. It SHALL end an upload that fails with `PARTS_INVALID` or `CIPHERTEXT_SHORT` as abandoned in the same way. It SHALL add the staged counts to its final JSON when the import carries them.

#### Scenario: A published member limit

- **WHEN** the listing carries `max_members` 1000 and the folder holds 1001 members
- **THEN** the CLI refuses before it creates an import and shows both numbers

#### Scenario: An older server

- **WHEN** the listing has no `limits` key
- **THEN** the CLI checks against its built-in defaults

#### Scenario: Unpublished limits

- **WHEN** the listing carries `limits: null`
- **THEN** the CLI says that limits are not published and checks no member or space limit itself

#### Scenario: The server refuses a creation

- **WHEN** creation returns 422 `IMPORT_STORAGE_LIMIT` with a `limit`
- **THEN** the CLI fails in the register phase with the unit, the limit and the requested amount
- **AND** it does not say to run the same command again

#### Scenario: The upload parts do not fit the copy

- **WHEN** the issued parts do not cover the encrypted copy
- **THEN** the CLI removes its checkpoint and says to cancel the open import and start a new run
