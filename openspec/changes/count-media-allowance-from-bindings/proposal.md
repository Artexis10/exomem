## Why

Offload checks the media allowance by walking every note of the vault (`media_allowance.reference_usage`), and it walks while it holds the vault's mutation boundary. On an installed service, measured on 2026-11-27, offload admission held the boundary for 39 s at p50, 104 s at p90 and 209 s at most. While offload holds it, every save waits, and a save that waits 5 s is refused with `MUTATION_BUSY`.

The walk runs under the boundary at six places:

- the offload scan, once per scan;
- admission before encryption (`_reuse_walk_inputs`);
- the protection check (`media_store_protect`);
- the publication check (`media_store_publish`), which a direct `offload()` call always walks again;
- the refusal check that preserve reaches through `enqueue_offload`, and the refusal status;
- `take_census`, which a status with a demand and an upload refusal call.

The batch census of 2026-10-30 (`OffloadBatch`) cut the walks to about one per batch. Each walk still holds the boundary for its whole length, and each media worker process starts a new batch.

`add-cloud-media-store` D10 allows a cache of the owner total once its cost is measured and it has an explicit invalidation contract. The cost is now measured. The physical binding projection (`media_bindings`), which eviction already keeps, has the contract: a checkpoint that every change moves.

## What Changes

- Offload, the offload scan, the refusal status, upload admission and the window status read the owner total from the binding projection, in one ledger read outside the boundary. None of them walks the notes while it holds the boundary.
- Under the protection and publication holds, offload uses the total only while the projection is current. When the projection's checkpoint moved since offload read the total, offload reads the total again with one indexed query, not a walk.
- A projection that is not current makes offload return the job to the queue without spending an attempt (`MEDIA_STORE_BUSY`). Status reports the total as being counted.
- A rebuild that fails records, durably, up to five notes with their reasons. Until a change follows that failure, the total is unknown and names those notes, as the census does today.
- The projection counts what the census counts. A side table keeps every confirmed displaced reference, and every page that names a stored version but binds no original. A displaced reference counts. A page that binds no original makes the total unknown and names the page.
- Every vault with an allowance keeps the projection, not only a vault with pooled storage or a protection policy. In file mode the service withdraws the projection at each start, so an edit made while the service was stopped is counted.
- In store mode, a rebuild publishes at the frontier of the snapshot that it read, and the audit replay brings the projection to the head. Under the publication hold, offload replays a lagging audit suffix of at most one batch (256 rows) instead of waiting.
- The batch census goes: `OffloadBatch`, `ReferenceUsage.charge`, the `pointer_replacements` marker and the recorded owner census (`media_owner_census`) are removed. Each publication reaches the projection through its own governed write.
- This change amends `add-cloud-media-store`'s requirement "The media allowance is checked before bytes arrive" in place, with a dated marker. The batch-census sentence becomes a pointer to this change, and the batch scenario moves here.

A vault with no allowance keeps the path that `#3382` gave it: no total is read.

## Capabilities

### Modified Capabilities

- `cloud-media-store`: offload reads the owner total from the binding projection and never walks the notes under the mutation boundary. A projection that is not current makes offload wait without spending an attempt. Three requirements are added. The capability exists only in the active change `add-cloud-media-store`, so this change adds requirements and amends that delta in place, rather than copying a requirement of about 2,700 characters into a `MODIFIED` block.

## Impact

- Code: `media_allowance.py`, `media_bindings.py`, `media_store.py`, `media_worker.py`, `import_windows.py`, and the service start path that runs the first offload scan.
- Stored data: the media ledger gains a side table, an index on `(sha256, size)`, a version-key index, and two meta keys: the side table's generation and the last failed rebuild. Every vault with an allowance gains the projection's rows. Canonical notes do not change.
- Behaviour: saves no longer wait for an offload census. After a change that the projection cannot follow exactly, such as an outside edit, offload waits for a background rebuild, and status says so. In file mode a vault with an allowance rebuilds the projection once per service start, outside the boundary.
- Cost per save: a vault with an allowance now pays the projection's per-write work that a pooled vault already pays. The design's cost section gives the figures and their budgets.
- Residual: in file mode, a vault whose companions change faster than one rebuild takes can keep offload waiting until a quiet window. The design's failure modes name the trigger for a follow-up.
- Undo: revert the code. The old code ignores the new table, indexes and keys, and walks again. The design's migration section covers an upgrade after a revert.
