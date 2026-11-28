## Why

Offload checks the media allowance by walking every note of the vault (`media_allowance.reference_usage`), and it walks while it holds the vault's mutation boundary. On an installed service, measured on 2026-11-27 on a vault with about 17,500 pages in its catalogue, offload admission held the boundary for 39 s at p50, 104 s at p90 and 209 s at most. That vault ran in file mode at the time and moved to store mode on 2026-11-28. The walk reads the disk in both modes, so the hold is the same in store mode. While offload holds the boundary, every save waits, and a save that waits 5 s is refused with `MUTATION_BUSY`.

The walk runs under the boundary at six places:

- the offload scan, once per scan;
- admission before encryption (`_reuse_walk_inputs`);
- the protection check (`media_store_protect`);
- the publication check (`media_store_publish`), which a direct `offload()` call always walks again;
- the refusal check that preserve reaches through `enqueue_offload`, and the refusal status;
- `take_census`, which a status with a demand and an upload refusal call.

The batch census of 2026-10-30 (`OffloadBatch`) cut the walks to about one per batch. Each walk still holds the boundary for its whole length, and each media worker process starts a new batch.

`add-cloud-media-store` D10 allows a cache of the owner total once its cost is measured and it has an explicit invalidation contract. The cost is now measured. In store mode the physical binding projection (`media_bindings`), which eviction already keeps, has the contract: the store's audit trail names every changed page, and the projection's checkpoint records how far it has followed that trail.

## Scope

This change covers store mode only. `make-vault-store-canonical` D15 makes store mode the default and then the only canonical mode. In file mode no audit trail names the changed pages, so a projection there needs its own invalidation paths and a rebuild at each start. A vault in file mode keeps today's owner census until file mode stops being a canonical mode. Its boundary hold during offload stays as a known limit, which this change does not fix.

## What Changes

- In store mode, offload, the offload scan, the refusal status, upload admission and the import window status read the owner total from the binding projection, in one ledger read outside the boundary. While offload holds the boundary, it walks no notes for the allowance, and it replays at most one batch of 256 audit rows or fewer. Upload admission follows the audit trail before it decides, so a lagging projection never admits an upload that does not fit. In file mode, upload admission keeps today's census.
- Under the protection and publication holds, offload uses the total only while the projection is current. When the projection's checkpoint moved after offload read the total, offload reads the total again with one indexed query.
- Before each hold, offload follows the audit trail until the projection lags the store by one batch or less.
- A projection that is not current makes offload return the job to the queue without spending an attempt (`MEDIA_STORE_BUSY`). Status reports the total as being counted.
- A note that the projection cannot count, such as one that does not parse, makes the total unknown and names the note, while the projection stays published. A rebuild that cannot read a source records up to five notes in the media ledger, so the media worker process can read them. An unrelated save neither clears a named note nor starts a rebuild.
- The projection counts what the census counts. The census's per-page rule moves into one function that the census and the projection both call. A side table keeps what the binding rows cannot hold: every confirmed displaced reference, which counts, and every page whose stored reference is malformed, or that names a stored version but binds no original, which makes the total unknown and names the page.
- The projection also counts the pages that the census walks but the store does not own, such as a sync conflict copy. The rebuild reads them, and a check at each store sweep updates their rows. A save during that walk never withdraws the projection.
- Every vault in store mode with an allowance keeps the projection, not only a vault with pooled storage or a protection policy. The store sweep runs the projection's recovery at each reconcile interval, on the watcher's thread, so no rebuild runs in the startup sequence.
- A rebuild publishes at the audit position of the snapshot that it read, and the audit replay brings the projection to the head.
- In store mode, the projection is current once it has followed the audit trail to its head. Today it also compares the store revision, which the derivation drain raises after every save with no audit row. So today the projection is not current after any save until a whole rebuild, which also stalls pooled eviction in store mode. This change fixes both. Eviction still stops on a page on which today's rebuild fails, and the drain names that page. The eviction stall comes from reading the code, and a task of this change shows it.
- The custody seam of the projection stops raising. It withdraws the projection only for a displaced page, which the trail cannot express as binding rows. A page that does not parse gets an unknown row that stops eviction, as today's rebuild does. A transient read failure leaves the projection lagging. A raising seam today invalidates the scope of every seam.
- In store mode, the batch census goes: `OffloadBatch`, `ReferenceUsage.charge`, the `pointer_replacements` marker and the writes of the recorded owner census (`media_owner_census`). File mode keeps them until it retires.
- The code pull request amends `add-cloud-media-store`'s requirement "The media allowance is checked before bytes arrive" in place, with a dated marker. Its batch-census sentence then applies to file mode only, and its batch scenario moves here.

A vault with no allowance keeps the path that `#3382` gave it: no total is read.

## Capabilities

### Modified Capabilities

- `cloud-media-store`: in store mode, offload reads the owner total from the binding projection and walks no notes under the mutation boundary. A projection that is not current makes offload wait without spending an attempt. Three requirements are added. The capability exists only in the active change `add-cloud-media-store`, so this change adds requirements, and the code pull request amends that delta in place. A `MODIFIED` copy of that requirement, about 2,700 characters long, would exceed the requirement length limit.

## Impact

- Code: `media_allowance.py`, `media_bindings.py`, `media_store.py`, `media_worker.py`, `import_windows.py` and the store sweep in `file_watcher.py`.
- Stored data: the media ledger gains a side table, a covering expression index on the version key, an index on `(sha256, size)`, and two meta keys: the side table's generation and the last failed rebuild. The job table gains the time of a job's first `MEDIA_STORE_BUSY` return, so the wait's age survives a restart. Every vault in store mode with an allowance gains the projection's rows. Canonical notes do not change.
- Behaviour: in store mode, saves no longer wait for an offload census. After a change that the trail cannot express, such as a migration, offload waits for a background rebuild, and status says so. Pooled eviction no longer stalls after each save. File mode does not change.
- Cost per save: in store mode, a vault with an allowance now pays the projection's work after each save, which a pooled vault already pays. The design's cost section gives the work and its budgets.
- Undo: revert the code. The old code ignores the new table, indexes and keys, and walks again. The design's migration section covers an upgrade after a revert.
