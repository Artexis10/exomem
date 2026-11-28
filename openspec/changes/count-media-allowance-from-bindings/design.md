## Context

Offload admits an original against the media allowance in three steps:

1. **Admission**, before encryption: the owner total and the versions of equal bytes that offload may reuse (`_reuse_walk_inputs`).
2. **The protection hold** (`media_store_protect`): the total again, before the upload name is protected.
3. **The publication hold** (`media_store_publish`): the total again, after the upload and before the reference is written.

Each step reads the owner total with `media_allowance.reference_usage`. That function walks the header of every note under the Knowledge Base, and it takes the consistency guard for the whole walk, so the walk holds the vault's mutation boundary. The offload scan, the refusal check that preserve reaches through `enqueue_offload`, the refusal status and `take_census` take the same walk. Measured on an installed service on 2026-11-27, offload admission held the boundary for 39 s at p50, 104 s at p90 and 209 s at most. Saves wait behind it, and the epistemic graph's watcher cannot withdraw its index while it waits.

The batch census (`OffloadBatch`, 2026-10-30) shares one walk across a run of members. It keeps the walk's hold, and each media worker process starts a new batch.

The physical binding projection (`media_bindings`) already holds one row per bound original: its companion, digest, size and the reference in the selected store. Eviction and the retained-store drain read it. Its checkpoint (`Checkpoint`) moves on every change: a withdrawal adds one to the generation and clears completeness; an exact delta applies the change and restores completeness; a rebuild walks the pages outside the boundary and publishes under a brief hold. `current()` holds only when the projection is complete, at the current policy and, in store mode, at the store's revision and audit head.

Load target: 20,000 pages (`make-vault-store-canonical`) and 100,000 visual rows (`add-face-search`). A binding is one companion page, so this design proves its costs at 100,000 bindings.

## Goals / Non-Goals

**Goals**

- No walk of the notes runs while the boundary is held, on a vault with an allowance.
- The allowance decides as the census decides, and offload reuses a version only when the census would have offered it.
- Each change that can bind or release a version reaches the owner total before offload relies on it.

**Non-goals**

- The allowance's rules, the refusal codes and the status fields stay as they are.
- A vault with no allowance keeps the path that `#3382` gave it.
- Eviction's use of the projection does not change, apart from the shared rebuild changes in D5 and D6.

## Decisions

### D1. The owner total comes from the binding projection

Offload reads the owner total in one ledger read transaction, outside any boundary. The read returns:

- the checkpoint and the side table's generation (D3);
- the last failed rebuild, if one is recorded (D5);
- the total: the sum of plaintext sizes over the distinct versions `(store, object, version)` that the binding rows and the side table's displaced rows name in the selected store;
- a conflict flag: one version named with two digests, sizes or references, as `media_allowance._consistent` refuses;
- the reuse candidates: up to 8 distinct versions (`_REUSE_CANDIDATE_LIMIT`) with the original's digest, size and key version, each with one companion outside Trash that is not displaced.

A version-key index (a stored column or an expression index) lets the aggregate read the index without a sort. An index on `(sha256, size)` serves the reuse slice. The candidates then pass the existing chain: `damaged_among`, `_live_filing`, `_head_failure`, the protection hold's re-check and `has_quarantined`. A damaged candidate leaves this read's slice and still counts, as `skip_reuse` does today. A candidate whose filing no longer matches is skipped, and the protection hold confirms the one that offload keeps.

The offload scan, the refusal check, the refusal status, upload admission and the window status read the same total, once, before they take their own holds. `take_census` and `recorded_census` become this read.

### D2. The checks under the boundary

Under the protection hold and the publication hold, offload:

1. checks that the projection is current, and otherwise follows D5 (D6 in store mode);
2. uses the total it read when the checkpoint equals the one it read with;
3. reads the total again with the one aggregate when the checkpoint moved, and decides on that total.

Neither hold reads a note for the allowance. A refusal that the scan or the refusal check keeps stands until the next scan, as `#3382` made it for a refusal recorded during the scan's walk.

### D3. The projection counts what the census counts

| Case | Census | Projection |
|---|---|---|
| Live and Trash companions in the selected store | counted | binding rows with a reference |
| Another store | skipped | reference is NULL, not counted |
| One version named by several pages | once | once, by the version key |
| One version with conflicting facts | unknown, page named | conflict flag: unknown, up to five pages named |
| Confirmed displaced page | every page counted | today one row per original: a displaced version can be lost (below) |
| `stored_object` with no binding | unknown, page named | today `from_page` returns None: the page is missed |
| Retired source companion | no reference | no reference |

`_fill_displaced` keeps one row per original, because `binary_rel` is the primary key, and a canonical companion's row wins. A confirmed displaced page that names another version than the kept row is lost, so the projection under-counts. A side table, keyed by companion path, now keeps two kinds of page that the binding rows cannot hold:

- **every confirmed displaced reference**, which counts. The media ledger records one version per original path (`recorded_versions`), so every confirmed page of one original names the same version.
- **every page that names a stored version but binds no original**, which makes the total unknown and names the page.

Keeping the second kind in the side table, rather than raising in `from_page`, leaves eviction and the retained-store drain unchanged.

A rebuild fills the side table. A delta replaces the side rows of each page that it writes or deletes: it removes them, and adds an unbound row when the page now names a stored version but binds no original. A delta that reads a displaced page raises `DisplacedReference`, which is a `ValueError`, and that withdraws the projection until a rebuild. Each delta and rebuild writes the generation that the side table matches. The allowance reads the projection as current only when that generation equals the checkpoint's (see Migration and rollback).

Where the projection is stricter, the rebuild fails where the census counts. Examples are two canonical companions that bind one original, an extant companion that lost its binding, a Trash retirement whose witness no longer holds, and a page of another store whose placement cannot be resolved. Each makes the total unknown and names the page, and none makes it smaller. Eviction already stops on each of them.

In store mode the projection reads the store's pages, so it also sees companions that the store has not written to disk yet. The census's disk walk can miss them.

### D4. Every vault with an allowance keeps the projection

- `_tracks_bindings` is true when a valid allowance is configured (`media_allowance.configured`), as it is for pooled storage or a protection policy.
- `recover` runs before the offload scan whenever the vault tracks bindings, not only with pooled storage. It runs in the serving process: at the start scan and at the watcher's periodic reconcile (300 s, 900 s when quiet). The media worker process never rebuilds.
- In file mode, the serving process withdraws the projection at each start, before the first scan, on a vault with an allowance. The file-mode frontier has no revision or audit position, and the watcher's seed compares nothing, so no other path sees an edit made while the service was stopped.
- A restore that replaces pages outside governed writes withdraws the projection when it ends.
- A change of the allowance, the store or the policy changes the projection's policy key, which makes the projection not current until a rebuild.

### D5. A projection that is not current: wait, or unknown

The record of a failed rebuild moves from process memory (`_failed_census`) to the media ledger. It holds the generation that the failure left and up to five blockers (`BLOCKERS_SHOWN`), each reason cut at `REASON_MAX_CHARS`. `recover` keeps its rule: it does not repeat a failed rebuild until the generation moves.

The rebuild goes on past a note that fails on its own and collects it as a blocker, as the census does, and still publishes nothing. A walk that fails as a whole names no note.

When the projection is not current:

- **A failure is recorded at the current generation:** offload refuses with `MEDIA_ALLOWANCE_UNKNOWN` and the blockers, and keeps retryable demand, as the census does today.
- **Otherwise:** offload raises `MEDIA_STORE_BUSY`. The worker returns the job without spending an attempt, with the queue's existing backoff (60 s, doubling to 15 minutes). The refusal status reports the total as being counted.
- **Upload admission** admits the upload, as an unknown record does today. Offload checks again before it publishes.

### D6. Store mode

Every governed write in store mode reaches the projection through the custody follow (`follow_store`). The follow withdraws and applies one audit batch, so it moves the checkpoint even for a write that binds nothing. Between the store's commit and that follow, the projection lags the store and is not current.

- **Before each hold**, offload runs the follow, so the projection usually reaches the store's head before offload takes the boundary.
- **Under the protection and publication holds**, a projection that is complete and at the current policy, but behind the audit head by at most one batch (256 rows), replays that suffix under the hold. Offload then reads the total again. A longer suffix or a broken trail raises `MEDIA_STORE_BUSY`. The publication hold comes after the upload, so the replay keeps a lagging projection from wasting it.
- **A rebuild** publishes at the frontier (revision and audit position) of the snapshot whose pages it read, and the follow then replays the rows after it. Today a rebuild gives up when any write lands during its walk, so on a vault that saves often it can starve. The pages and the frontier come from one snapshot.

## Rejected alternatives

- **A walk outside the boundary with a change marker:** correct, but a walk of 39 s to 209 s loses its race to any save, and it still walks once per batch.
- **A recorded census, confirmed by a walk only near the allowance:** it needs a margin constant, and it still walks under the boundary near the limit.
- **Lending the boundary between chunks of the walk:** saves still wait for each chunk, and a write in any chunk restarts the census.
- **A running total that each delta keeps:** reading it costs nothing, but every delta must keep version reference counts right. It is the fallback if the aggregate misses its budget.
- **A second projection keyed by companion page:** it repeats the binding projection's derivation and deltas. The side table holds only the two kinds of page that the binding rows cannot.
- **Raising in `from_page` for a reference without a binding:** simpler, but it stops eviction and the retained-store drain on a page that blocks only the allowance.

## Correctness against concurrent writes and erasures

Each change that can bind or release a version reaches the projection as follows:

- **A governed write in file mode.** Examples are an offload publication, a reference confirmation, a copy again of a damaged pointer, a release, a Trash move or recovery, a move or rename, and a companion rewrite by extraction, governance or backfill. The write withdraws the projection before it installs and applies the exact delta after (`begin_file_change`, `finish_file_change`, or `operation` for a compound move). It does both inside its own boundary hold. Offload's checks hold the same boundary, so they never see half a write. A write between two checks moves the checkpoint, and the second check reads the total again.
- **Offload's own publication** is such a governed write, so the next member's read includes the version. This replaces `ReferenceUsage.charge`.
- **A copy again of a damaged pointer** in the replica worker is such a governed write too. This replaces the `pointer_replacements` marker.
- **A governed write in store mode** follows D6.
- **An outside edit in file mode.** The watcher withdraws the projection when the event arrives, before its debounce (`_record_external_locked`). For a lost event, the periodic reconcile finds the drift and withdraws, within 300 s (900 s when quiet). For an edit while the service was stopped, the start withdraw covers it (D4). A vault served without a watcher sees an outside edit at its next start.
- **An outside edit in store mode.** The store's sweep imports it as an audit row, which the follow replays.
- **A restore** follows D4.
- **Erasures.** Deletion, emptying Trash, `forget_original` and the release paths of offload and eviction are governed writes. Their delta removes rows, and the total falls. A total read before an erasure is higher than the truth, so at worst it refuses. The check under the hold reads the lower total.
- **A configuration change** changes the policy key (D4).

An outside writer can still change a page between the event and its withdrawal. The census has the same window: D10 states that its witnesses "do not create an atomic snapshot against external writers". The gap that `add-cloud-media-store` accepted on 2026-11-09 shrinks. An owner's own edit that binds a version no other companion names was under-counted until the batch ended. It is now under-counted only until its event arrives, or for one reconcile interval when the event is lost.

## Cost

**Per offload member, outside the boundary:** one ledger read: the checkpoint, one aggregate over the version-key index, at most 8 reuse rows by index, and the side table, which holds few rows.

**Per member, under each of the two holds:**

- reads of the checkpoint and the frontier, which cost the same at any size;
- one aggregate when the checkpoint moved;
- in store mode, a replay of at most 256 audit rows when the projection lags: one body parse per changed page.

No note is read for the allowance. Today each hold can include a walk of every note's header.

**Budgets:**

- The aggregate takes at most 50 ms at p95 at 100,000 bindings on a generated ledger. If it misses, the fallback is the running total.
- A replay of 256 rows under a hold takes at most 0.5 s at p99. That is the Records bound for one boundary hold by import work, used here as the bound for a background hold.

**Per write, for a vault with an allowance and no pooled storage or policy.** These vaults now pay what a pooled vault already pays:

- **File mode:** the current bytes of each written note, a front matter parse and one ledger query by path. A companion write adds two durable ledger transactions.
- **Store mode:** each custody follow reads the audit suffix and the changed pages, takes the boundary briefly and writes two ledger transactions.

The budget is at most 10 ms at p95 of added work per write, timed around the projection's calls. The save p95 of `scripts/semantic_write_latency.py` is recorded before and after on the same machine.

**Rebuild:** it reads every page outside the boundary. Its publication holds the boundary while SQL replaces the rows and merges the cache rows, which costs one pass over the bindings. The budget is at most 0.5 s at p99 at 100,000 bindings. A rebuild runs after a withdrawal, at each start in file mode, and after a configuration change. It stages rows in SQLite temporary tables, as it does today.

## Failure modes and who pays

| Failure | What happens | Who pays |
|---|---|---|
| Projection not current after a change | Offload waits, the job returns with its attempt refunded, status says the total is being counted | The owner: originals stay local until the rebuild, so volume use stays higher |
| File-mode rebuild loses each race to companion writes, such as a processing backlog that rewrites companions | Offload waits until a quiet window as long as one rebuild | The owner's volume. A follow-up (below) bounds it |
| Rebuild fails on notes | `MEDIA_ALLOWANCE_UNKNOWN`, up to five notes named | The owner repairs the notes, as today |
| A case where the projection is stricter (D3) | Unknown where the census counted, page named | The owner repairs the page |
| An outside edit not yet seen | An offload can pass the allowance by the versions that the edit bound | The operator's storage, bounded by that edit |
| A displaced version whose ledger confirmation changed | Stays counted until the next rebuild, which then names the page | The owner is refused early |
| Unreadable ledger | Total unknown | As today |
| Per-write work | Each save pays up to the per-write budget | Every save |

Follow-up trigger: when an installed service shows offload waiting more than one hour for a file-mode rebuild, a follow-up makes the rebuild re-read the paths that changed during its walk instead of giving up.

## Migration and rollback

- No canonical note changes. `media_bindings.initialize` adds the side table, the two indexes and the two meta keys with `CREATE ... IF NOT EXISTS`.
- On the first start after the upgrade, a vault with only an allowance has no complete projection. `recover` rebuilds it with one walk outside the boundary, and offload waits until the rebuild publishes. A vault that already tracks bindings has no side-table generation yet, so its allowance waits for one rebuild. Its eviction is unaffected.
- `media_owner_census` is no longer written or read. The table stays, because a downgrade reads it.
- **Revert:** the old code ignores the new table, indexes and keys, and walks again. Its status shows the last figure recorded before the upgrade until its first batch census.
- **Upgrade after a revert:** the old code either moves the generation without the side table's generation, on a vault that tracks bindings, or follows nothing, on a vault with only an allowance. In file mode the start withdraw covers both. In store mode the frontier check and the side-table generation do. Either way the allowance reads the projection as not current until a rebuild.

## What this supersedes in `add-cloud-media-store`

- D10, "Complete reference accounting": the owner meter no longer reads the sidecars under the consistency guard. It reads the projection outside it, and the rebuild reads the pages outside the boundary. The rebuild uses the same managed root and reserved-path reader, so no second exclusion list exists.
- D10's "it adds no inventory database": the projection is the inventory. Eviction added it, and this change adds a side table to it.
- D10's "a cache requires measured cost and an explicit invalidation contract": the cost was measured on 2026-11-27, and the checkpoint is the contract.
- The 2026-10-30 amendment "One census per batch", its list of writers and the gap accepted on 2026-11-09 are superseded by D2 and the correctness section above.
- The requirement "The media allowance is checked before bytes arrive" is amended in place, with a dated marker: its batch sentence points here, and its batch scenario moves to this change. D10 and tasks 4.5.1 and 5.4.9 get a pointer to this change when the code merges.
