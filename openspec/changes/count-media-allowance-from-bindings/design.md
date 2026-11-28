## Context

Offload admits an original against the media allowance in three steps:

1. **Admission**, before encryption: the owner total and the versions of equal bytes that offload may reuse (`_reuse_walk_inputs`).
2. **The protection hold** (`media_store_protect`): the total again, before the upload name is protected.
3. **The publication hold** (`media_store_publish`): the total again, after the upload and before the reference is written.

Each step reads the owner total with `media_allowance.reference_usage`. That function walks the header of every note under the Knowledge Base on disk, in both modes, and it takes the consistency guard for the whole walk, so the walk holds the vault's mutation boundary. The offload scan, the refusal check that preserve reaches through `enqueue_offload`, the refusal status and `take_census` take the same walk. Measured on an installed service on 2026-11-27, on a vault with about 17,500 pages in its catalogue, offload admission held the boundary for 39 s at p50, 104 s at p90 and 209 s at most. That vault ran in file mode then and moved to store mode on 2026-11-28. Saves wait behind the walk, and a save that waits 5 s is refused.

The batch census (`OffloadBatch`, 2026-10-30) shares one walk across a run of members. It keeps the walk's hold, and each media worker process starts a new batch.

The physical binding projection (`media_bindings`) holds one row per bound original: its companion, digest, size and the reference in the selected store. Eviction and the retained-store drain read it. Its checkpoint records a generation, completeness, the policy, and in store mode the store revision and audit position it has followed. In store mode, every write of a page records an audit row (`vault_store/write.py:1237`), and `follow_store` (`media_bindings.py:522`) replays those rows into the projection. `current()` (`media_bindings.py:709-715`) holds only when the projection is complete, at the current policy, and at the store's revision and audit head. After any save, the derivation drain raises the store revision with no audit row, so that comparison fails until a whole rebuild. D8 drops the revision from it.

Load target: 20,000 pages (`make-vault-store-canonical`) and 100,000 visual rows (`add-face-search`). A binding is one companion page, so this design proves its costs at 100,000 bindings.

## Scope

This change covers store mode only. `make-vault-store-canonical` D15 makes store mode the default and then the only canonical mode. In store mode the audit trail names every changed page, so the projection follows changes without its own invalidation paths. In file mode no trail exists: a projection there would need a withdraw at each start, a reconcile path for lost watcher events, and rebuilds that race companion writes. A vault in file mode keeps today's owner census and `OffloadBatch` until file mode stops being a canonical mode. **Known limit:** in file mode, offload still holds the boundary for the census walk. This change does not fix it.

Offload chooses its path by the store mode that it reads before admission. A mode change before a later hold makes that hold return the job with `MEDIA_STORE_BUSY`.

## Goals / Non-Goals

**Goals**

- In store mode, no walk of the notes runs while offload holds the boundary, on a vault with an allowance.
- The allowance decides as the census decides, and offload reuses only a version that the census would have offered.
- Each change that can bind or release a version reaches the owner total before offload relies on it.

**Non-goals**

- The allowance's rules, the refusal codes and the status fields stay as they are.
- A vault with no allowance keeps the path that `#3382` gave it.
- File mode does not change.
- Eviction's use of the projection does not change, apart from the shared changes in D3, D4, D6, D7 and D8. D8 also fixes an existing stall of eviction in store mode.

## Decisions

### D1. The owner total comes from the binding projection

Offload reads the owner total in one ledger read transaction, outside any boundary. The read returns:

- the checkpoint and the side table's generation (D3);
- the last failed rebuild, if one is recorded (D4);
- the total: the sum of plaintext sizes over the distinct versions `(object, version)` that the binding rows and the side table's counted rows name (below);
- a conflict flag: one version named with two digests, sizes or references, as `media_allowance._consistent` refuses;
- the unknown rows of the side table, up to five, with their reasons;
- the reuse candidates (below).

**The aggregate.** A binding row holds a reference only in the selected store: `from_page` sets it to NULL for another store (`media_bindings.py:319-320`). A change of store changes the policy key, so the projection is not current until a rebuild. The aggregate therefore has no store filter:

```sql
SELECT COUNT(*), COALESCE(SUM(size), 0), COALESCE(MAX(conflict), 0) FROM (
  SELECT MIN(size) AS size,
         MIN(size) <> MAX(size) OR MIN(sha256) <> MAX(sha256)
           OR MIN(reference) <> MAX(reference) AS conflict
  FROM media_bindings
  WHERE reference IS NOT NULL
  GROUP BY json_extract(reference, '$.object'), json_extract(reference, '$.version'))
```

A partial covering expression index serves it: `(json_extract(reference, '$.object'), json_extract(reference, '$.version'), size, sha256, reference) WHERE reference IS NOT NULL`. SQLite then reads the index in order, with no sort and no table lookup. Task 3.4 records the plan on SQLite 3.45, the system SQLite that CI runs. Every reference is written by `json.dumps` (`media_bindings.py:326`), so the expressions never fail on a row.

The side table's counted rows are few, so offload sums them separately. It adds each distinct version among them that no binding row names, with one index lookup per version. A lookup that finds the version with another size, digest or reference sets the conflict flag. An index on `(sha256, size)` serves the reuse slice.

**Reuse candidates.** Up to 8 distinct versions (`_REUSE_CANDIDATE_LIMIT`) whose binding row has the original's digest, size and key version (`$.key_version`), one row each. The query excludes two kinds of row:

- a displaced row, which `_fill_displaced` keeps when no canonical row exists. Its companion has a counted row in the side table, so a `NOT EXISTS` on the side table's companion path excludes it.
- a companion under Trash, by the Trash folder's path prefix.

The census excludes both kinds from reuse today. The candidates then pass the existing chain: `damaged_among`, `_live_filing`, `_head_failure`, the protection hold's re-check and `has_quarantined`. A damaged candidate leaves this read's slice and still counts, as `skip_reuse` does today.

**New behaviour: a stale candidate is skipped.** Today, a candidate whose filing no longer matches makes the whole census unknown for that admission (`media_store.py:1631-1632` and `2366-2367`). Now offload skips the candidate and tries the next one, because the total comes from the projection, and the hold reads it again when it moved.

The offload scan, the refusal check, the refusal status, upload admission and the import window status read the same total, once, before they take their own holds. In store mode `take_census` and `recorded_census` become this read.

### D2. The checks under the boundary

Before each of the protection and publication holds, offload runs `follow_store` until the projection lags the store's audit head by 256 rows or fewer. It stops after 8 rounds, which cover a burst of 2,048 writes. A vault that writes faster than that is busy enough that offload yields: the job returns with `MEDIA_STORE_BUSY`.

Under each hold, offload:

1. checks the store mode, which must equal the mode read at admission;
2. when the projection is complete, at the current policy and behind the audit head by 256 rows or fewer, replays that suffix once;
3. checks that the projection is current (D8), and otherwise follows D5;
4. uses the total it read when the checkpoint equals the one it read with;
5. reads the total again with the one aggregate when the checkpoint moved, and decides on that total.

Under a hold, offload walks no notes for the allowance, and it replays at most one batch of 256 audit rows. A refusal that the scan or the refusal check keeps stands until the next scan, as `#3382` made it for a refusal recorded during the scan's walk.

### D3. The projection counts what the census counts

**One per-page rule.** The census's per-page rule (`media_allowance.bound_references`, the block at `media_allowance.py:231-260`) moves into one function that the census walk, the rebuild and every delta call. For a page, it answers: no reference; a counted reference, with its binding and whether it is displaced; or unknown, with a reason.

**The side table** is keyed by companion path. It holds what the rule answers for a page whenever the binding rows cannot hold it:

- **counted:** every confirmed displaced reference, also when `_fill_displaced` keeps a binding row for its original; and a reference that the rule counts on a page whose binding `from_page` does not derive.
- **unknown:** every failure that the rule can tie to one page:
  - a page that does not parse;
  - a stored reference that is malformed (`media_allowance.py:240-241`);
  - a reference in the selected store with no valid binding (`media_allowance.py:246-247`);
  - a displaced reference that the ledger does not confirm;
  - the four cases where the projection is stricter than the census (below).

A page whose canonical binding row holds the counted reference gets no side row. Every confirmed displaced page gets a counted side row, so D1's reuse slice excludes the displaced binding row by its companion. `from_page` today returns None for a page with `stored_object` but no binding (`media_bindings.py:289-294`), and `_fill_displaced` (`media_bindings.py:871`) keeps one row per original, so either kind would be missed without the side table.

**An unknown row does not stop publication.** The rebuild goes on past the page, records the row and publishes. The allowance then reads the total as unknown and names up to five such pages (D5). A governed fix of the page replaces its row through the delta, with no rebuild.

**The eviction flag.** An unknown row carries a flag that is set exactly when today's rebuild raises on its page:

- `from_page` raises (`media_bindings.py:289-316`): a page that does not parse, an incomplete identity, an unsafe path, a placement that cannot be resolved (`trash_metadata.py:446-480`), or an incomplete recovery reference on a bound page;
- `_confirm_displaced` raises on a displaced reference that the media ledger does not record (`media_bindings.py:864-865`);
- `_retire_census` raises on a Trash retirement whose witness no longer holds (`media_bindings.py:879-916`);
- the rebuild raises on an extant companion that lost its binding (`media_bindings.py:811-824`), or on two companions that bind one original (`media_bindings.py:834-835`).

The flag covers only these deterministic faults, which raise `ValueError` or `UnicodeError`. Every other class is a race or a read failure, never a page fault:

- `OSError`, `sqlite3.Error`, `held_fs.HeldFsError`, and a store read error (`CollectionStoreError`, `CollectionStoreUnavailable`; the classes that `_policy_ready` already catches, `media_bindings.py:994-995`);
- a reserved-path read error other than `MISSING`;
- in store mode, a disagreement between the snapshot's record of a placement and its bytes on disk (below);
- a Trash placement that changed during the read. `PlacementCensus.require_current` raises it as a `ValueError` today (`trash_metadata.py:482-487`, called from `media_bindings.py:306-307`). The code raises it as a distinct type, `PlacementChanged`, a subclass of `ValueError` so that today's handlers still catch it, and the classifier tests that type before the `ValueError` class. No rule matches message text.

In a rebuild, such an error fails the whole rebuild, which D4 records and retries. In a follow or a delta, it leaves the projection lagging (D7), and the next follow replays the rows.

**Where a page or a binary is read.** In store mode, the per-page rule asks the follow's or the rebuild's snapshot about every file that the store owns, anywhere in its call graph. Disk alone answers only for a file that the store does not own.

- **A page that the store owns** is read from the snapshot, by the pattern of `store_read.owned_bytes` (`trash_metadata.py:575-581`).
- **Whether a binary that the store owns exists**, with its hash and size, comes from the snapshot's binary records. `store_binaries` holds the path, hash, size and revision of each binary (`collection_store/schema.py:1026-1031`). Every binary write records its audit row in the commit (`vault_store/write.py:1370-1402`). The snapshot answers one path by primary key and many in batches (`vault_store/read.py:304-345`). This covers `object_key`'s check of a pooled original (`media_bindings.py:231-235`) and the checks of the old original path in `retired_source` (`trash_metadata.py:436-443`) and `_retire_census` (`media_bindings.py:896-902`).
- **A `.meta.json` placement** is a binary that the store owns (`vault_store/ownership.py:92`). The snapshot records its hash but not its bytes, so its bytes come from disk and must match that hash.

For a plain binary, the snapshot record is the whole answer: the check never touches disk and hashes no original. Only a placement is read from disk and compared with its recorded hash. A placement that the snapshot records but disk lacks or holds with other bytes, or one that disk holds without a record, is a race. A Trash move shows how. Its commit records the moved placement, and the files move only after the commit (`delete_directory.py:898`). A rename that fails stays for the projector (`vault_store/binaries.py:144-145`, `:207`), which writes no audit row. Without this rule, a follow would read the moved Trash page from the snapshot and find no placement on disk. It would then record a flagged row for a placement without trust (`trash_metadata.py:480`), which no later audit row clears. With it, the race leaves the projection lagging in a follow, and the next follow replays the rows; once the projector has moved the file, the replay succeeds. In a rebuild, the race is a whole failure, and D4 retries it at the next sweep. Each race names its placement's path: the follow logs it as the reason that it lags, and the rebuild records it as a blocker. So a disagreement that never resolves names its file.

Two consequences for pages:

- `_retire_census` reaches its read of the source companion (`media_bindings.py:910`) only when the snapshot holds that companion (`:894-895`, `:903-904`). A disk copy that lags it, missing or with an older body that lacks the witness, fails today's rebuild, and under the new classifier it would get a flagged row by mistake. The snapshot read removes both.
- `from_page` reaches `placements.retired_source`, then `_confirmed_by_trash` and `reference_companion` (`trash_metadata.py:525-551`), which reads the Trash companion from the snapshot. A Trash companion that the store has deleted is then absent, so the delta does not take the source as retired, whatever disk still holds.

In `require_current`'s reopen of a placement (`trash_metadata.py:484`), `MISSING` means that the placement changed, a race classed with `PlacementChanged`.

`from_page` returns None for an unbound page, also when its reference is malformed (`media_bindings.py:291-294`), so such a page's row has no flag. Nor does a row of a page that the store does not own. `current()`, which eviction and the retained-store drain call, is false while a flagged row exists, so they stop where they stop today. When the drain census stops there (`media_store.py:4786`), it names up to five flagged pages, as it names blockers after a failed rebuild (`media_store.py:4766-4781`).

**Rebuild and deltas.** A rebuild fills the side table. A delta replaces the side rows of each page that it writes or deletes. It touches no other page's row, except for two companions that bind one original (below). A delta that reads a displaced page withdraws the projection until a rebuild, through D7. A delta that reads a page with a deterministic fault, such as one that does not parse, deletes that page's binding rows and writes its unknown row with the eviction flag set, with no withdraw. Each delta and rebuild writes the generation that the side table matches. The allowance reads the projection as current only when that generation equals the checkpoint's.

**Pages the census skips.** The census walk skips reserved families and linked files (`reserved_paths.py:3161-3168`). The store owns some reserved families as pages (`vault_store/ownership.py:57`, `:72`), so the rebuild and the deltas skip every page in a reserved family, as the census does.

**Pages the store does not own.** The census walks three kinds of page that the store does not own (`vault_store/ownership.py:82-110`), so the audit trail never names them:

- a page under a dot-named folder;
- a sync conflict copy;
- a page in a Records or Planning layer.

The rebuild and a check at each store sweep (D4) read these pages on disk, outside the boundary, with the census's tree reader and the per-page rule. They store only side rows, never binding rows, for such a page. The walk works as follows:

1. It reads the header of each page that the store does not own, and lists the other entries without reading them.
2. It passes `entry_changed` (`reserved_paths.py:3035-3041`), because without it any save during the walk ends the walk (`reserved_paths.py:3319-3327`).
3. It ignores a reported change to a path that the store owns, which the audit trail covers.
4. After the walk, it reads each changed page that the store does not own once more.

The check compares the side rows it read with the published ones. It replaces the rows that differ under the publication guard, as a delta does. Like a delta, it moves the checkpoint's generation and writes the side table's, so a rebuild that read the pages before it discards its result (`media_bindings.py:831`). A check that fails, because a page kept changing or a read failed, keeps the projection and its residual window, and runs again at the next sweep. A walk race never withdraws the projection. In a rebuild, a page that kept changing gets an unknown row, which the next sweep's check replaces.

Between two sweeps, a change to such a page is not counted. The collection store, which writes the Records and Planning layers, never writes a stored reference (no `collection_store/` module names `stored_object`). So a stored reference on such a page comes from an outside writer, such as a sync tool or a hand copy.

**Where the projection is stricter.** Today's rebuild fails where the census counts in four cases:

- two canonical companions that bind one original;
- an extant companion that lost its binding;
- a Trash retirement whose witness no longer holds;
- a page of another store whose placement cannot be resolved.

Each now gives its page an unknown row. The total is unknown and names the page, and none of the cases makes it smaller. Eviction already stops on each of them. Two companions that bind one original get a row each, which names the original. A delta that writes or deletes either of them reads again every page whose row names that original.

### D4. Tracking and recovery in store mode

- `_tracks_bindings` (`media_bindings.py:102`) is also true in store mode when a valid allowance is configured (`media_allowance.configured`). In file mode it keeps today's rule.
- **Where recovery runs.** The watcher's reconcile thread runs `media_bindings.recover` after each store sweep's import (`file_watcher.py:2044-2058`), at each reconcile interval (300 s, 900 s when quiet). After the sweep at start, it runs once the seed is published (`file_watcher.py:2829-2833`), so a rebuild never delays activation. In store mode the watcher's media reconcile does not run (`file_watcher.py:1916-1917`), so today `recover` runs only from the start scan and only for a pooled vault. Only the serving process rebuilds. The media worker process never does.
- **No rebuild in the startup sequence.** In store mode, `enqueue_offloads` (`media_store.py:1280`) follows the trail and no longer runs `recover`. The start scan (`server_runtime.py:1372`) therefore waits for no rebuild, and the first sweep after start rebuilds. In file mode, `enqueue_offloads` keeps today's `recover`.
- **Follow before rebuild.** In store mode, `recover` reads the audit head when its pass starts and follows the trail to that head, round after round, with no round bound. It then ends the pass, and the next sweep follows the rows that landed meanwhile. It reads each round outside the mutation boundary and takes the boundary only to publish that round, as `follow_store` does today (`media_bindings.py:554-566`). Only offload's follow before a hold keeps D2's bound of 8 rounds. `recover` then rebuilds only for a reason that the projection owns:
  - the projection is not complete;
  - it records another policy, as after a configuration change, because `policy_key` covers the whole media configuration (`media_bindings.py:200-210`);
  - it records no audit position, as a pooled vault's projection from file mode does;
  - the audit row at its recorded position is not the one that it followed, or no identity record exists, as after an upgrade from the code before this change (D8);
  - the side table's generation differs from the checkpoint's, as when the side table is new or the old code wrote the projection;
  - the follow cannot advance, because it withdrew the projection (D7, D8).

  The first three make `follow_store` return early without a withdraw (`media_bindings.py:531-532`). A projection that only lags the head, a flagged unknown row (D3) and a pending-outside mark (D8) never start a rebuild. A round that loses its publication race to another writer of the projection ends the pass without a rebuild, and the next pass follows again. Today `recover` rebuilds whenever the projection is not current (`media_bindings.py:937-942`).
- **The record of a failed rebuild** moves from process memory (`_failed_census`) to the media ledger, where the media worker process reads it.
  - A rebuild fails as a whole on a race or a read failure, the classes that D3 lists. It may come from the store snapshot, the media ledger, a Trash placement read (`trash_metadata.py:403-418`), the walk of pages that the store does not own, or a Trash placement that changed during the walk. Such a failure is recorded and retried, as below. A deterministic fault of one page, a `ValueError` or `UnicodeError` including the displaced handling, is an unknown row instead (D3).
  - The record holds the generation and up to five blockers (`BLOCKERS_SHOWN`), which status and offload's refusal name (D5). A blocker is a note or file that the rebuild could not read, with its reason cut at `REASON_MAX_CHARS`.
  - In store mode, `recover` retries a whole failure at the next sweep, so at most one rebuild runs per sweep interval. It also retries when the generation moved, and once after start for a record that an earlier service process wrote.
  - A named blocker clears only when a rebuild publishes, and a named unknown row clears only through a rebuild or a delta on its page. An unrelated save does neither and starts no rebuild. In store mode a save reaches the projection only through the follow (`media_bindings.py:735-737`), which leaves a projection that is not complete alone (`:531-532`), so the save does not move the generation.
  - In file mode, `recover` keeps today's rule: it retries when the generation moved, and once after start.

### D5. A total that is not current: wait, or unknown

When offload reads the total:

- **A failed rebuild is recorded and not yet due for a retry (D4):** offload refuses with `MEDIA_ALLOWANCE_UNKNOWN` and the blockers, and keeps retryable demand, as the census does today.
- **The projection is current, and an unknown row or a conflict makes the total unknown:** offload refuses in the same way and names up to five pages.
- **The projection is not current for another reason:** offload raises `MEDIA_STORE_BUSY`. The worker returns the job without spending an attempt, with the queue's existing backoff (60 s, doubling to 15 minutes). The refusal status reports the total as being counted.
- **Upload admission** admits the upload, as an unknown record does today (`upload_refusal`, `media_store.py:1637`). Offload checks again before it publishes.

### D6. A rebuild publishes at its snapshot's audit position

A rebuild holds one store snapshot from its start through `_retire_census` and the lost-binding check. It reads the store pages, the store's audit head, the binary records and every owned page that those two steps read from that snapshot. Today the snapshot closes when the generator of `_pages` ends (`media_bindings.py:590-599`), at the end of the walk (`:793`), and `_retire_census` (`:896-914`) and the lost-binding check (`:818-824`) run after it. It publishes at that audit position, and the follow then replays the rows after it. Its race check requires the checkpoint and the policy that it started from, and that its snapshot's audit row is still the same row. The head may have moved (D8). Today a rebuild gives up when any write or any derivation drain lands during its walk (`media_bindings.py:831`), so on a vault that saves often it can starve.

### D7. The custody seam withdraws only for a displaced page

`repair_paths` (`media_bindings.py:725`) catches the errors that its own derivation raises and returns, instead of raising out of the custody seam:

- **A displaced page:** the trail cannot express the change as binding rows, so the seam withdraws the projection. The rebuild that follows records the page (D3).
- **A page with a deterministic fault (D3), such as one that does not parse:** the delta deletes the page's binding rows and writes its unknown row with the eviction flag set. The seam withdraws nothing.
- **A race or a transient read failure, of the classes that D3 lists, such as a Trash placement that changed or a store read error:** the seam withdraws nothing and records nothing. The projection lags and is not current, and the next follow replays the rows (D2, D4).

Today such an error leaves the custody seam, and a seam that raises makes `apply_receipt_paths` invalidate the scope of every seam (`freshness.py:2497-2523`). An error in this optional projection should cost only the projection.

### D8. The audit head is the store frontier

In store mode, `current()` compares the projection's recorded audit position with the store's audit head. It no longer compares the store revision, and it no longer reads the pending-outside mark (below). Its frontier test holds only what the projection owns: the projection is complete, records the current policy and records the audit head as its position, and the audit row at that position is the one that it followed (below). Each caller adds what it needs:

| Caller | What it requires |
|---|---|
| `recover` (`media_bindings.py:937`) | The frontier test without the head, the side table's generation and an identity record (D4). Lag behind the head is followed, not rebuilt |
| Offload's allowance checks, before and under the holds (D2) | The frontier test and the side table's generation. An unknown row makes the total unknown instead (D5) |
| Eviction (`media_store.py:3693`, `:3969`) | The frontier test and no flagged unknown row (D3) |
| Status and eviction candidates, through `_cache_selection` (`media_store.py:4034`) and `_occupancy_paths` (`:4056`) | As eviction |
| The retained-store drain (`media_store.py:4786`, `:4803`) | As eviction |
| The offload scan, through `unreferenced` (`media_bindings.py:964`) | As eviction |
| Protection, through `_policy_ready` (`media_bindings.py:992`) | As eviction |

Every caller of the eviction kind stops on a flagged row where it stops today after a failed rebuild. Status counts pooled bytes as not reclaimable, as its queries do while the projection is not current (`media_store.py:4026-4027`). The offload scan walks every companion instead (`media_store.py:1315-1318`). Protection keeps today's stop: a failed rebuild today leaves the projection incomplete, so `_policy_ready` is false and protection planning reads no policy keys (`media_bindings.py:1001`, `:1025`). A flagged row keeps it false.

**The pending-outside mark.** In store mode, the watcher hands an outside edit of a store page to the importer and returns before it marks anything (`file_watcher.py:1635-1636`, `:1656-1657`). So the audit trail, not the mark, names that edit. The marks left in store mode fence the access policy (`file_watcher.py:1637-1647`) and derived indexes, such as a failed graph recovery (`file_watcher.py:2221-2223`). None of them changes a page body or the media configuration, so the projection need not wait on them. Waiting would also stall it: in store mode only the access-policy reconcile clears the mark (`file_watcher.py:1824-1830`, `:2198`), because the store sweep returns before the reconcile that retries it (`file_watcher.py:1916-1917`). In file mode, every caller keeps today's check of the mark.

The frontier comparison replaces the revision in three places:

- the follow's check at the head (`media_bindings.py:538-539`), which then returns true without recording;
- the follow's check before it publishes (`media_bindings.py:555`), which requires the checkpoint and the policy that it read, and that the audit row at the position it publishes is still the same row;
- the rebuild's race check (`media_bindings.py:831`), which requires the checkpoint, so its generation, and the policy that it started from, and that its snapshot's audit row is still the same row.

In the last two, the head may have moved: the follow replays the rows after the published position (D6).

The follow still withdraws when the recorded revision or audit position is ahead of the store's (`media_bindings.py:535-537`), as a restore of an older database shows.

**The identity of the last followed row.** Each follow and rebuild also records the audit row at its recorded position: its `seq`, revision, time (`at`) and path. It writes that record in a meta key of its own, in the transaction that writes the checkpoint. The follow, also at the head, reads the row at the recorded `seq`, one primary key lookup in the snapshot that it already holds. It withdraws when that row differs, when the record names another `seq` than the checkpoint, or when no record exists. `current()` makes the same lookup and is false on a difference, so a restored head that lands exactly on the recorded position is not taken as current. A separate key keeps the checkpoint's format, which the old code parses strictly and resets when it meets an unknown field (`media_bindings.py:153`, `:161-162`).

**Why the revision cannot be the frontier.** After each commit's derivation, the derivation drain raises the store revision with no audit row (`vault_store/derivation.py:747-748`). Each commit queues that derivation (`vault_store/write.py:988`). Rederive raises it once per batch (`vault_store/rederive.py:296`). At the audit head, the follow records nothing and returns false unless the revisions are equal (`media_bindings.py:538-539`). So after any save, the projection stays not current until a whole rebuild, and that rebuild publishes nothing when a drain lands during it.

**The invariant.** Every change to a store page's path or body appends an audit row:

- A governed commit writes each page and its audit row in one transaction: `_put_page`, `_delete_page` and `_move_page` (`vault_store/write.py:1280-1367`) each record through `_Trail` (`vault_store/write.py:1237`). `canonical-vault-store` requires one audit row per file in that transaction. The sweep's import commits the same way.
- A migration loads the pages before it turns store mode on (`vault_store/migrate.py:194`). The step that turns it on also records an audit row without a path (`vault_store/migrate.py:807-817`), on which the follow withdraws.
- A rollback empties the pages and records an audit row without a path in the same transaction (`vault_store/rollback.py:460-467`).
- Every other write to `store_pages` changes only `mtime_ns`: `vault_store/projector.py:1172`, `vault_store/migrate.py:801` and the schema step at `collection_store/schema.py:1089`. The projection does not read it. The `store_pages` table of `media_capacity.py:95` is another table, in the media capacity ledger.

Within one store database, the audit position never repeats. `seq` is `AUTOINCREMENT` (`collection_store/schema.py:1059`), triggers refuse to update or delete an audit row (`collection_store/schema.py:367-379`, `:1072`), and a rollback keeps the trail (`vault_store/rollback.py:28`). A restore of an older copy of the store database brings back its `seq` counter, so later commits can reuse values that the projection already followed. While the restored head is below the recorded position, the follow withdraws (`media_bindings.py:535-537`). Once commits pass it, the row at the recorded `seq` is another row, and the identity check withdraws. The projection reads only page paths and bodies from the store (`media_bindings.py:548`, `:597`), and no derived row. Task 1.9 proves the invariant.

**Existing behaviour that this fixes.** Pooled eviction and the retained-store drain call the same `current()`. In store mode, after each save's derivation, eviction keeps every candidate (`media_store.py:3969`) and does not retire a shared cache proof (`media_store.py:3693`). The drain census refuses (`media_store.py:4786`, `:4803`). This lasts until `recover` rebuilds the whole projection (`media_bindings.py:937-942`), which runs only when an offload scan or a protection pass calls it, and the next save starts it again. This finding comes from reading the code. No test or service has shown it yet. Task 3.12 shows it.

## Rejected alternatives

- **Waiting for the trail to name a blocker before a failed rebuild runs again:** dropped, because no remaining blocker can appear in the trail. A fault of a page that the store owns is an unknown row, a snapshot error names no note, a plain binary never touches disk, a placement race settles with no audit row, and a page that the store does not own never appears in the trail (D3).
- **A walk outside the boundary with a change marker:** correct, but a walk of 39 s to 209 s loses its race to any save, and it still walks once per batch.
- **A recorded census, confirmed by a walk only near the allowance:** it needs a margin constant, and it still walks under the boundary near the limit.
- **Lending the boundary between chunks of the walk:** saves still wait for each chunk, and a write in any chunk restarts the census.
- **A running total that each delta keeps:** reading it costs nothing, but every delta must keep version reference counts right. It is the fallback if the aggregate misses its budget.
- **A second projection of every page's reference:** it repeats the binding projection's rows and deltas. The side table holds only the pages that the binding rows cannot.
- **Raising in `from_page` for a reference without a binding:** simpler, but it stops eviction and the retained-store drain on a page that blocks only the allowance.
- **Recording the new revision when the follow finds no new audit row:** it keeps the revision in the comparison, which the store does not guarantee to move only with pages. The audit head is the frontier that the store's own contract guarantees (D8).
- **A projection in file mode too:** it needs a withdraw at each start and a rebuild that races companion writes, for a mode that is being retired.

## Correctness against concurrent writes and erasures

Each store-mode change that can add or remove a stored reference reaches the projection as follows:

- **A governed write of a page the store owns** records an audit row per path (`vault_store/write.py:1237`). The follow reads every Markdown path of the batch from the pinned snapshot (`media_bindings.py:546-549`) and applies the binding rows and the per-page rule. This covers:
  - offload publication and reference confirmation, which replace `ReferenceUsage.charge`;
  - a copy again of a damaged pointer, which replaces the `pointer_replacements` marker;
  - releases, Trash moves, recovery and emptying Trash;
  - moves and renames;
  - companion rewrites by extraction, governance and backfill;
  - an owner's edit through the write tools, including a malformed reference or one without a binding.
- **Writes in `_trash`, `_archive`, `_attachments` and `_Adoption`.** The store owns these folders (`vault_store/ownership.py:94`), so their writes are in the trail. The follow does not depend on custody calling the seam for such a path, which the index's excluded-folder rule can skip (`index_sync.py:1984`). The audit head check (D8) makes the projection not current until a follow, and offload's own follow (D2) replays the row.
- **An outside edit of an owned file, an edit made while the service was stopped, and a restore of the vault's files.** The store sweep imports each as an audit row (`file_watcher.py:2044-2058`).
- **A migration and a rollback** record an audit row without a path (`vault_store/migrate.py:814`, `vault_store/rollback.py:464`). The follow withdraws on it (`media_bindings.py:543-545`). A projection that the vault kept in file mode, as a pooled vault does, has no audit position, so the follow never reads that row. `recover` rebuilds it, because it is not current (D4).
- **A restore of the store's database.** The follow withdraws when the store's revision or audit head is behind the checkpoint (`media_bindings.py:535-537`), or when the row at the recorded position is another row (D8).
- **A derivation drain and a rederive** raise the store revision and change no page's path or body (D8), so the projection stays current.
- **Pages the store does not own** follow D3.
- **A configuration change** changes the policy key (`media_bindings.py:200-210`). The follow leaves such a projection alone (`media_bindings.py:531-532`), so it stays not current, and the next `recover` rebuilds it (D4).
- **Erasures.** Deletion, emptying Trash, `forget_original` and the release paths of offload and eviction are governed writes. Their delta removes rows, and the total falls. A total read before an erasure is higher than the truth, so at worst it refuses. The check under the hold reads the lower total.

Offload's holds and the follow's publication take the same boundary, so a check never sees half a delta. A write between two checks moves the checkpoint, and the second check reads the total again.

`add-cloud-media-store` accepted a gap on 2026-11-09: an owner's own edit that binds a version no other companion names was under-counted until the batch ended. In store mode that gap closes for an edit through the write tools, which is counted before the next hold. A disk edit by an outside editor is counted after the next store sweep.

## Cost

**Per offload member, outside the boundary:**

- the follow rounds of D2, each a store snapshot read of up to 256 audit rows and their pages' bodies, a parse per page, a brief boundary hold and two ledger transactions;
- one ledger read: the checkpoint, one aggregate over the covering index, at most 8 reuse rows by index, and the side table, which holds few rows.

**Per member, under each of the two holds:**

- reads of the checkpoint, the store's audit head, the audit row at the recorded position and the mode, which cost the same at any size;
- one aggregate when the checkpoint moved;
- a replay of at most 256 audit rows when the projection lags: one body parse per changed page and two ledger transactions.

No note is walked for the allowance. Today each hold can include a walk of every note's header.

**Per save, for a vault in store mode with an allowance and no pooled storage or policy.** These vaults now pay what a pooled vault already pays. After each save's commit, the custody follow:

- reads the audit suffix and the changed pages' bodies from a pinned store snapshot;
- parses each changed page's front matter with `from_page` and the per-page rule, and queries the ledger by path;
- takes the mutation boundary a second time (`media_binding_audit_publish`, `media_bindings.py:554`);
- writes two ledger transactions: the withdraw at `synchronous=FULL` (`media_bindings.py:190`), and the apply.

Every save pays this, whether or not it touches a companion, because each audit batch moves the checkpoint.

**Left-out pages:** at each store sweep, one walk on the watcher's reconcile thread, outside the boundary. It lists every entry under the managed root, one directory read per folder, and reads the header of each page that the store does not own. Its time grows with the number of such pages, not with the owned pages. The measured census read about 17,500 headers in 39 s at p50, about 2 ms per header. By that estimate, 1,000 such pages take about 2 s plus the listing. Task 3.4 measures the walk. The sweep already walks every owned file at the same interval, and saves do not wait for either walk.

**Rebuild:** one streaming read of the store's pages and the left-out pages, outside the boundary. Its publication holds the boundary while SQL replaces the rows and merges the cache rows, which costs one pass over the bindings. In store mode a rebuild runs after the upgrade, after a withdrawal and after a configuration change, on the watcher's reconcile thread and never in the startup sequence (D4).

**Budgets and fallbacks:**

| Measure | Budget | Fallback if missed |
|---|---|---|
| Owner-total aggregate at 100,000 bindings on a generated ledger, on SQLite 3.45 | p95 ≤ 100 ms | A running total that each delta keeps |
| Replay of 256 audit rows under a hold | p99 ≤ 0.5 s | A smaller batch under the hold; the pre-hold follow keeps 256 |
| Rebuild publication hold at 100,000 bindings | p99 ≤ 0.5 s | Diff publication: compute the difference from the published rows outside the hold; under it, check the checkpoint and write only the difference |
| Added save p95 under concurrent saves | ≤ 10 ms | A batch whose delta changes no row moves the cursor in one transaction, with no withdraw |

The 0.5 s bound is the Records bound for one boundary hold by import work (`openspec/specs/records/spec.md:790`), used here for a background hold.

## Failure modes and who pays

| Failure | What happens | Who pays |
|---|---|---|
| Projection not current, such as after a migration, a broken trail, a displaced page or a configuration change | Offload waits, the job returns with its attempt refunded, status says the total is being counted | The owner: originals stay local until the rebuild, so volume use stays higher |
| The follow cannot catch up before a hold | `MEDIA_STORE_BUSY` with the queue's backoff | The owner's volume, while the vault writes faster than 2,048 rows per member |
| A rebuild fails as a whole | `MEDIA_ALLOWANCE_UNKNOWN`, with up to five notes or files named. The rebuild runs again at the next sweep, after a generation move, or once after start | The owner, while the source stays unreadable |
| A placement on disk disagrees with the snapshot, as after a Trash move whose rename the projector still owes | A follow lags and logs the placement's path. A rebuild fails as a whole, names the placement and runs again at the next sweep (D3) | Offload waits until the projector moves the file |
| A note that the projection cannot count, including the stricter cases (D3) | The projection publishes, and the total is unknown and names the note. Eviction stops where it stops today | The owner repairs the note, and the fix's delta clears it |
| A page the store does not own changes between two sweeps | An offload can pass the allowance by the versions that the page binds | The operator's storage, bounded by that page, for one interval |
| Such a page keeps changing during the sweep's walk | The check keeps the projection and runs again at the next sweep | The operator's storage, for one more interval |
| A displaced version whose ledger confirmation changed | Stays counted until the next rebuild, which then names the page | The owner is refused early |
| The custody seam meets a displaced page | The projection is withdrawn, and other seams keep their scope (D7) | The owner waits for the rebuild |
| The custody seam meets a page with a deterministic fault (D3), such as one that does not parse | The page gets a flagged unknown row, and eviction stops as today (D7) | The owner repairs the page, and the fix's delta clears it |
| The custody seam meets a transient read failure | The projection lags, and the next follow replays the rows (D7) | Offload waits until that follow |
| Unreadable ledger | Total unknown | As today |
| Per-save work | Each save pays up to the per-save budget | Every save |

**Instrument for waiting.** Offload logs `media_offload_wait` at every `MEDIA_STORE_BUSY` return that this change adds, and once more at the job's publication or refusal. Each event carries the seconds since the job's first such return, the number of returns, the reason for the return and the outcome. The job row stores the time of the first return, so the age survives a restart of the media worker or the service. Publication or refusal clears it. When an installed service shows a wait above one hour, a follow-up finds which withdrawal kept the projection from being current.

## Migration and rollback

- No canonical note changes. `media_bindings.initialize` adds the side table, the two indexes and the two meta keys with `CREATE ... IF NOT EXISTS`, and a third meta key for the identity of the last followed audit row (D8). The job table gains a nullable column for the time of the first return, added as its other columns are (`media_jobs.py:1100-1145`). Every job insert names its columns, so the old code ignores it.
- On the first start after the upgrade, a vault in store mode with only an allowance has no complete projection. The first store sweep's `recover` rebuilds it from the store on the watcher's reconcile thread, outside the boundary and after the seed is published. Offload waits until the rebuild publishes. A vault that already tracks bindings has no side-table generation yet, so its allowance waits for one rebuild. Its first follow finds no identity record and withdraws the projection (D8), so its eviction also waits for that one rebuild.
- A vault that migrates from file mode to store mode records an audit row without a path. A projection that follows the trail withdraws on it. A projection that the vault kept in file mode, as a pooled vault does, has no audit position, so the follow leaves it alone, and `recover` rebuilds it because it is not current. A vault that rolls back to file mode uses the census again.
- In store mode `media_owner_census` is no longer written or read. The table stays, because file mode and a downgrade read it.
- **Revert:** the old code ignores the new table and keys and walks again. It maintains the expression index when it writes `media_bindings`. Its status shows the last recorded figure until its first batch census. Its `current()` compares the store revision again, so its eviction stalls after saves as it does before this change.
- **Upgrade after a revert:** either the old code followed the trail without the side table's generation, on a vault that tracks bindings, or it followed nothing, on a vault with only an allowance. In the first case, the side-table generation and the identity record, which the old code does not move, make the projection not current until a rebuild. In the second, the old code never tracked the projection (`media_bindings.py:102-119`, `:933`), so its checkpoint and identity record still match the trail. The projection is not current until it follows to the head, with no rebuild.

## What this supersedes in `add-cloud-media-store`

For store mode only:

- D10, "Complete reference accounting": the owner meter no longer reads the sidecars under the consistency guard. It reads the projection outside it, and the rebuild reads the pages outside the boundary. The rebuild skips the same reserved families, so no second exclusion list exists.
- D10's "it adds no inventory database": the projection is the inventory. Eviction added it, and this change adds a side table to it.
- D10's "a cache requires measured cost and an explicit invalidation contract": the cost was measured on 2026-11-27, and the audit trail with the checkpoint is the contract.
- The 2026-10-30 amendment "One census per batch", its list of writers and the gap accepted on 2026-11-09 are superseded by D2 and the correctness section above.

The code pull request amends the requirement "The media allowance is checked before bytes arrive" in place, with a dated marker. Its batch-census sentence then applies to file mode, and its batch scenario moves to this change. The same pull request adds a pointer to this change to D10 and to tasks 4.5.1 and 5.4.9.
