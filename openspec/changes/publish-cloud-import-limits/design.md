## Context

See proposal.md. This change is the cellctl, image, CLI and pin side of the paired Substrate change `admit-cloud-imports-against-published-limits`. That change defines the C5 columns, the admission order and the Home flow.

Current state, read from source on 2026-10-26:

- The cell image fixes the member count (500,000), the free-space reserve (256 MiB), names (255 bytes) and paths (4096 bytes). The Job takes no limit flags, and its argument parser refuses an unknown one.
- cellctl fixes the part size (64 MiB), the 10,000-part capacity, the 24-hour upload window and the 2-hour import hold (`import_deadline`).
- The size cap `max_cell_gib` (20) sits inside `LocalStorage`. `build_storage_config` returns no local settings when `CELLCTL_CELL_STORAGE` is unset, so a fleet with only Hetzner volumes has no cap.
- A cell's `storage_gib` is part of the render digest that the pod template carries. A raise therefore re-renders and restarts the cell pod. A comment in the reconcile code says a raise bumps no generation. The Substrate 0071 trigger does bump it, so the comment is stale.
- The storage receipt writes `observed_storage_capacity_bytes` and `observed_storage_generation`. It treats a claim in `Resizing` or `FileSystemResizePending` as not done.
- cellctl writes only `recipient`, `upload_id`, `part_urls`, `urls_expire_at`, `observed_state` and `error_code` on an import. The Job's `{files, bytes}` report is never stored.
- The prepare Job runs under a hold, with the runtime at zero replicas, before `key_ready`.
- The deployed cellctl reads cells through an explicit column list and imports through a filter to its known fields. Additive columns do not reach it. The Cloud owner confirmed that `db.py` in the deployed v0.141.0 image matches this reading.

## Goals / Non-Goals

**Goals:**

- Publish the limits that cellctl and the Job enforce, from one source each.
- Grow a volume for an import without an operator, allowing the brief restart that the render digest causes.
- Record what the Job staged, and let Home and the CLI read it.

**Non-Goals:**

- Plan-storage entitlement enforcement and the paid-storage baseline. The operator deferred both on 2026-10-26.
- Adopted counts in C5.
- Chart settings for the member limit and the reserve.
- A change to the path and name limits. The Job keeps enforcing them.

## Decisions

### D1. cellctl publishes the limits it enforces

An `ImportLimits` value in `imports.py`, beside `PART_BYTES`, builds the object. Each key has one source:

| Key                              | Source                                                          |
| -------------------------------- | --------------------------------------------------------------- |
| `max_members`, `reserve_bytes`   | The values that cellctl passes to the Job (D4)                   |
| `space_factor`                   | A cellctl constant, 2                                           |
| `max_storage_gib`                | The cell's size cap and its node's room (below)                  |
| `part_bytes`, `max_parts`        | `PART_BYTES` and the 10,000-part capacity                        |
| `upload_window_seconds`          | `UPLOAD_DEADLINE`                                               |
| `import_deadline_seconds`        | `ReconcileConfig.import_deadline`                               |
| `import_growth_deadline_seconds` | `ReconcileConfig.import_growth_deadline` (D3)                    |

The module constants stay only as defaults for the standalone verify and the CLI fallback.

`max_storage_gib` is per cell, and its current size is `max(storage_gib, grown_storage_gib)`:

- On a Hetzner cell, it is the larger of the cap and the current size.
- On a local cell, it is the larger of the current size and the largest size, up to the cap, that `pool_takes` allows on the cell's node. That puts the node's room inside the published number. A node that publishes no reading allows nothing, so the value is the current size.

A paid cell that already exceeds the cap therefore never loses an import to the cap alone.

cellctl compares the object with the stored `import_limits` in the pass that reads the cell, and writes only when they differ. The cells touch trigger does not change, so the write bumps no generation and sends no notification. A local cell's value follows its node's free bytes, so cellctl recomputes it only in a pass that reads the node's room.

*Rejected: constants in Substrate.* A cap that the operator changes would need a Substrate release, and a local cell's room depends on its node.

### D2. One cap for every storage class

`max_cell_gib` moves from `LocalStorage` to the top level of `StorageConfig`, with the same default of 20 and the same check that it is at least `default_cell_gib`. `plan_growth` and the published `max_storage_gib` read it. A fleet with only Hetzner volumes now has a cap.

The `CELLCTL_CELL_STORAGE` JSON accepts the new top-level key and the earlier key inside `local`. The chart gains a top-level value and keeps the earlier nested value. cellctl refuses to start when both are set and differ. A silent choice between two caps either refuses imports that fit or grows a pool past its reserve. The cost is a failed rollout that the operator fixes in one value.

### D3. The import waits for growth before it holds the cell

`decide_import` waits while `recipient` is unset, the import has `required_storage_gib`, and either of these holds:

- the cell's observed capacity is below the requirement, or `observed_storage_generation` does not equal the cell's generation;
- the storage receipt shows a resize pending.

While it waits, the decision takes no hold and renders no prepare Job. The runtime pod stays mounted. A hold would stop the pod with `FileSystemResizePending` set, and the resize could then never finish.

The raise restarts the pod once, because of the render digest. The wait reads observed capacity and the claim's conditions, never pod readiness. A pod that is starting or terminating counts as waiting. The restart also lets an expansion that completes only at mount time finish, so waiting before the prepare Job holds in either expansion mode.

The wait ends at `import_growth_deadline`, counted from the import's `created_at`. The default is 60 minutes, above `backup_deadline`, so a backup that overlaps the wait does not use it up. `CELLCTL_IMPORT_GROWTH_DEADLINE_MINUTES` sets it, in the same way as the other deadlines. On expiry cellctl fails the import with `IMPORT_STORAGE_UNAVAILABLE` through the existing cleanup.

On a local cell, cellctl asks `pool_takes` again before it mints the key. When the node no longer holds the required size beside the snapshot reserve, cellctl fails the import with `IMPORT_STORAGE_NO_ROOM`. This is a refusal on purpose: an exhausted thin pool damages every cell on the node, so the wrong firing costs one retried import.

The check also runs before every larger local claim request, including cleanup and later ordinary reconciliation. Admission has already raised desired `storage_gib`, so checking only before the key is too late. Cellctl reads the last accepted claim request from the claim itself and retains it while the node cannot hold growth. It never lowers a claim, and desired `storage_gib` stays raised. Unknown node room leaves growth waiting; known insufficient room fails the import. The same rule covers a later render after import failure. The local cutover check proves this behavior before local import growth ships; Hetzner delivery can proceed independently.

No new observed state exists. Substrate derives `storage_ready` from the same receipt.

### D4. The Job enforces the published values

cellctl renders `EXOMEM_IMPORT_MAX_MEMBERS` and `EXOMEM_IMPORT_RESERVE_BYTES` into the import Job. The Job reads them and falls back to its defaults when they are absent. cellctl publishes the same two values, so the published number is the enforced number.

Environment variables carry the values because the Job's argument parser refuses an unknown flag. An older image ignores the variables and enforces its own defaults. Those equal the published defaults, because this change adds no chart setting for either value.

### D5. Staged counts

When the import Job succeeds, cellctl reads the `{files, bytes}` report that the Job already emits. It writes the immutable `staged_files` and `staged_bytes` pair before cleanup removes the successful Job's Pod report. A restart then preserves the report while cleanup and finalization keep their existing volume-release proof.

A missing or invalid report stays unknown and does not block cleanup. A later failure or cancellation may retain the raw pair. Public replies expose the pair only for `done` without pending cancel or discard intent. An import that finished earlier has no pair.

### D6. The CLI reads the limits

Before it creates an import, the CLI reads `GET /api/exomem/cloud/imports` and the `limits` it carries:

- A reply with no `limits` key comes from an older server. The CLI uses `DEFAULT_MAX_MEMBERS` and `DEFAULT_RESERVE_BYTES`, as it does today.
- A reply with `limits: null` means the cell published nothing. The CLI says that limits are not published and that the cell checks during the import. It applies no member or space limit of its own.
- A reply with an object gives the CLI `max_members` and `reserve_bytes` for its local checks.

The CLI maps `IMPORT_TOO_MANY_FILES`, `IMPORT_STORAGE_LIMIT` and `IMPORT_PARTS_LIMIT` to errors with `resume=False`, and it prints the typed `limit`. A rerun of the same folder would meet the same refusal. Each error names the phase of the refused call: creation is the register phase, and prepare-upload is the upload phase. `IMPORT_PARTS_LIMIT` leaves an open import. That error goes through `_abandoned`, as `PARTS_INVALID` and `CIPHERTEXT_SHORT` do, and it says to cancel the open import and start a new run. The CLI adds the staged counts to its final JSON when the import carries them.

### D7. Status rows carry `source_root`

`_compact_row` in `adoption_run.py` adds `source_root`, which the run already records. Scoped `status` accepts the existing `path` argument and returns `adopted_files` for that subtree. It counts distinct original paths with successful filing outcomes across all matching runs. Repeated and overlapping runs count each original once; per-run totals cannot supply that result. An incomplete scan returns an unknown aggregate, not a partial count. Home reads this complete aggregate, or shows the adopted count as unknown on older cells. This work stays in P7.

### D8. The contract pin

`substrate-contract.json` moves to the Substrate ref that holds migration 0079 and its grants. The deployed-cellctl tests run against that ref, so a drift between the grants and cellctl's writes fails there.

## Risks / Trade-offs

- **Entitlement.** The plan's storage limit is enforced nowhere, so growth on demand lets a cell of any plan reach the cap. The operator deferred the choice on 2026-10-26.
- **Hetzner online expansion has no production record.** Every cell class allows it, but nobody has raised a live volume. The rehearsal raises one real volume and records the capacity, the claim's conditions and any pod restart.
- **`space_factor` is an assumption.** The rehearsal measures the peak volume use of an import against `space_factor × declared bytes`.
- **An abandoned import keeps its grown volume.** A claim cannot shrink, and the cap bounds the cost.
- **A local cell's published maximum moves with its node's free bytes.** cellctl rewrites it only on change, and a write sends no notification.

## Migration Plan

Roll out outside the 02:00–05:00 UTC backup window.

1. Substrate deploys migration 0079 and the grants. Undo: leave the columns, which older code ignores.
2. Deploy this controller and the cell image through the ordinary cell rollout. Undo: return to the previous controller, and set `import_limits` to NULL to stop growth.
3. Substrate deploys admission and the Home flow. Undo: roll the deployment back. Volumes that grew stay grown.
4. Release the CLI. Undo: pin the earlier CLI, whose fallback works against older servers.

## Proof Plan

The workflow that changes is an import that needs more storage than the cell has. One check proves it: the cloud rehearsal creates such an import, and the volume grows with the pod mounted or restarted. The import then reaches `key_ready` and finishes, and its row carries the staged counts.

Controller tests cover the wait without a hold, a pod restart, the deadline failing before `key_ready`, the local no-room failure, and the written counts. A Job test shows that an environment limit refuses an archive one member over. CLI tests run against a synthetic API for a limits object, no `limits` key, null limits and each refusal. Strict OpenSpec validation and the privacy gate precede review.
