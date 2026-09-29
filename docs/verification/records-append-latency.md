<!-- authority:non-specification -->

# Records append latency: where the time goes

Harness: `scripts/measure-records-append-latency.py`. Raw numbers and cProfile output:
`records-append-latency-results.json` (baseline), `records-append-latency-after.json` (after the fixes,
including a 10,000-item run) and `records-storage-comparison-sqlite-prototype.json` (same directory).

The first half of this page is the baseline diagnosis. The fixes, their before/after numbers, the
file-versus-SQLite comparison and the recommendation follow it.

## Method

- Synthetic markdown-items collection with an `item_filename` and an `item_presentation` recipe,
  invented data, seeded at 10 / 100 / 1,000 items in a temp vault with a temp state root.
- Each measurement is one `record_memory` append through the real dispatcher
  (`writer_lease.invoke_command`): idempotency ledger, mutation boundary, post-commit fan-out.
- 30 guarded appends per size, p50 / p95. Before each, the client-side guard refresh
  (`record_memory inspect`, read the container hash), timed separately. A further 30 unguarded
  appends per size isolate the guard's own cost. One cProfile pass over an append and one over the
  refresh read.
- Stage timers are inclusive wall-clock wrappers (outermost entry only), so nested stages overlap
  and do not sum.
- Limits: embeddings, CLIP and file watcher are disabled, so embedding work is unmeasured (the
  fan-out reports it as `accepted`, i.e. deferred). The vault holds only this collection, so
  costs that scale with total vault size are understated. One 4-core Linux container.

## Findings (guarded append, p50 ms)

| Stage | N=10 | N=100 | N=1000 | Grows with N? |
|---|---:|---:|---:|---|
| **Total** | 355 | 710 | 4,600 (p95 5,078) | yes, about 4.3 ms per item |
| Dispatcher outside the leaf (ledger, egress filter, digest) | 6.8 | 7.8 | 6.4 | no |
| Idempotency ledger (`ledger.run` minus leaf) | 4 | 4 | 3 | no |
| Mutation boundary enter / exit | 2.2 / 0.4 | 2.2 / 0.4 | 1.8 / 0.4 | no |
| Request validation (`_validate_values`, twice) | 1.9 | 1.8 | 1.8 | no |
| Collection resolve (3 calls) | 7.9 | 7.4 | 8.0 | no |
| Manifest load + parse (parsed 9 times) | 85 | 80 | 78 | no (fixed cost) |
| Container-hash guard: snapshot read (`MarkdownItemsAdapter.read`) | 19 | 80 | 733 | **yes, O(N)** |
| Natural-key twins + after-hash + new item path | 0.6 | 1.9 | 15 | yes, small |
| Visibility check (`require_mutation_visibility`, 2 calls) | 24 | 89 | 777 | **yes, O(N)** |
| Pre-commit authorization (contains one visibility call) | 33 | 121 | 1,031 | **yes, O(N)** |
| Item presentation render (`render_markdown_item`) | 0.8 | 0.8 | 0.8 | no |
| `exomem-item-presentation` block alone | 0.08 | 0.09 | 0.09 | no |
| `batch_atomic_write` (guard rechecks + write + fan-out) | 249 | 418 | 2,400 | **yes, O(N)** |
| Post-commit fan-out inside it (index sync, self-write registration) | 133 | 125 | 133 | no |
| of which `index.upsert_after_write` | 93 | 92 | 86 | no |
| Due-state and sweep carriers, terminal housekeeping | 3 | 3.4 | 2.9 | no |
| **Guard refresh read** (`record_memory inspect`) | 279 | 946 | 7,605 (p95 8,088) | **yes, about 7.4 ms per item** |
| **Refresh + append round trip** | 634 | 1,656 | 12,205 | yes |
| Unguarded append (no `expected_container_hash`) | 487 | 923 | 4,765 | same as guarded |

Answers to the specific questions:

- **Does the container-hash guard read or hash the whole collection?** Yes. The container hash is
  the digest of the full item inventory, so `MarkdownItemsAdapter.read` reads, hashes and
  YAML-parses every item file on each append. The guard itself is not the marginal cost: an
  unguarded append is as slow, because the same snapshot is taken regardless.
- **Index sync.** Lexical, resolver and memory-refs sync run synchronously, about 86 ms and flat.
  Embeddings are `accepted` (deferred) and the graph is `registered` (deferred). Neither grows
  with N in this harness.
- **Ledger, fast-ack, response.** Together under 10 ms and flat. Fast-ack is a closure inside the
  ledger run and has no separate timer; its cost sits inside that 4 ms.
- **File write.** The write itself is not separable from the guard rechecks inside
  `batch_atomic_write`; the fan-out is 133 ms of the 2,400 ms at N=1000.
- **Off-path.** The lease renewer and the graph rebuild thread run alongside back-to-back
  appends. Their effect on p95 was not isolated.

## Top three hot spots

**1. Per-path authorization repeats policy and state resolution for every item.**
`record_governance.py:936` (`_authorize`) calls `egress.py:5228` (`release_level_for_path_only`)
once per item per pass. Each call reloads the governance policy (`policy.py:1292`), recomputes
tombstones (`lifecycle.py:1567`) and re-derives and stats the vault state directory
(`state_paths.py:303`, `:321`, `:278`). At N=1000 an append makes about 3,200 such calls, and the
refresh read about 7,000 (roughly 70% of the refresh profile). Callers: `record_formats.py:203`
(`_require_authorized`), `record_governance.py:2784` (`precommit_authorize_mutation`), `:2812`
(`require_mutation_visibility`), `records.py:1934`.
*Proposed fix:* resolve policy, tombstone set and state directory once per operation and evaluate
each path against that resolved object (`full_release_filter` already returns a per-call filter
that could carry it). *Expected gain (estimate):* removes most of the O(N) term in both calls,
so about 4.6 s to 2 s for the append and 7.6 s to 2.5 s for the refresh at N=1000.

**2. Directory censuses and guard rechecks run about 20 times per append.**
`vault.py:3296` (`_bounded_directory_entries`) ran 21 times and took about a third of the
profile. It classifies every entry through `reserved_paths.py:1996` (`classify_logical`, about
600k regex matches). `PathGuard.recheck` (`vault.py:1143`) re-reads and re-hashes each item file
(about 12 snapshot reads per item at N=1000, `vault.py:3556`). `batch_atomic_write`
(`vault.py:4618`) is 2.4 s of the 4.6 s.
*Proposed fix:* take one census per operation and reuse it for the visibility check, read and
pre-commit; cache classification per path within a census; recheck under the held mutation lock
by stat identity (inode, size, `mtime_ns`) instead of a full re-read. *Expected gain (estimate):*
`batch_atomic_write` pre-write cost from about 2.3 s to under 0.5 s at N=1000.

**3. The refresh read snapshots the collection three times.**
`record_governance.py:1406` (`inspect_collection`) calls `record_formats.py:395` (`read`) three
times (about 19 of 22.7 profiled seconds), and `records.py:1887` (`inspect_audit_gap`) adds a
further 7 s of audit-chain reads. A client pays this between every guarded append, so it is the
largest single number: 7.6 s of the 12.2 s round trip.
*Proposed fix:* one snapshot per inspect, shared by the inspection and the audit-gap pass; and a
per-file digest and parse cache keyed by stat identity so a snapshot rebuild only reads changed
files. A cheaper option for callers that only need the guard is a hash-only read. *Expected gain
(estimate):* refresh about 7.6 s to 2.5 s from the single snapshot alone.

Fixed cost, not O(N): about 350 ms per append at N=10. The manifest is parsed 9 times
(`structured_collections.py:1061`, about 80 ms) because `records.py:2738` and
`record_governance` resolve it repeatedly; passing the parsed manifest through would save about
70 ms. Index sync adds about 90 ms.

## What this does not explain

The reported 24-row backfill took about 21 minutes, roughly 52 s per row. The measured round trip
at 1,000 items is 12 s, and the collection cap is 2,000 items. So collection size alone, in this
harness, does not reach 52 s. Candidates that this harness does not cover: a larger real vault
(index and graph work that scales with vault size), embeddings enabled, slower storage, a larger
policy set, or concurrent load from the graph rebuild. Reproducing on a copy of the affected
collection, with `call_spans` enabled on the live service, would settle it.

---

# Fixes, before and after

All numbers are one append (or one guard refresh) through the real dispatcher, p50 / p95 over 30, in
milliseconds, on the same container as the baseline. Each row is cumulative. The 10 and 100 item rows
carry run-to-run noise of roughly 10 to 20 percent (one 657 ms p95 outlier at 10 items in the first
row is a noisy sample, not a regression). "Round trip" is guard refresh plus append at p50.

| Stage | Items | Append p50 | Append p95 | Refresh p50 | Refresh p95 | Round trip p50 |
|---|---:|---:|---:|---:|---:|---:|
| Baseline | 10 | 355 | 436 | 279 | 376 | 634 |
| | 100 | 710 | 859 | 946 | 1,115 | 1,656 |
| | 1,000 | 4,600 | 5,078 | 7,605 | 8,088 | 12,205 |
| 1. Authorization pass | 10 | 295 | 657 | 93 | 125 | 388 |
| | 100 | 575 | 650 | 336 | 376 | 911 |
| | 1,000 | 3,392 | 3,611 | 2,728 | 2,959 | 6,120 |
| 2. Stat-generation cache | 10 | 248 | 279 | 57 | 73 | 305 |
| | 100 | 306 | 374 | 136 | 185 | 442 |
| | 1,000 | 1,112 | 1,177 | 965 | 1,144 | 2,076 |
| 3. Batched rechecks, deduped censuses | 10 | 254 | 352 | 55 | 78 | 309 |
| | 100 | 249 | 309 | 120 | 178 | 369 |
| | 1,000 | 696 | 772 | 929 | 1,047 | 1,625 |
| 4. Manifest parse cache, one inspect snapshot | 10 | 156 | 175 | 34 | 46 | 190 |
| | 100 | 203 | 245 | 73 | 95 | 276 |
| | 1,000 | 628 | 696 | 464 | 599 | 1,093 |
| 5. Cheaper recheck, access memo | 10 | 157 | 195 | 31 | 46 | 188 |
| | 100 | 188 | 250 | 64 | 96 | 252 |
| | 1,000 | 625 | 706 | 416 | 546 | 1,041 |
| 5, collection caps lifted | 10,000 | 5,202 | 5,706 | 6,089 | 6,479 | 11,291 |

What each change is, and the red-first test that guards it (all in `tests/test_record_write_scaling.py`):

1. **Authorization pass** (`record_governance.py` `authorization_pass`, `governance/egress.py`
   `release_level_for_path_only(tombstones=...)`, `governance/lifecycle.py` `is_tombstoned_in`).
   Policy and the tombstone set are read once per operation instead of per item. Test: policy loads and
   tombstone scans per append and per inspect must not grow from 3 to 12 items. Red was 1 policy load
   and 3 tombstone scans per extra item on append, 10 of each on inspect.
2. **Stat-generation cache** (new `record_item_cache.py`, `vault.py` `PathGuard` policy `generation`,
   `record_formats.py` `read`). An item file whose (device, inode, size, mtime, ctime) is unchanged is
   served from memory: no open, hash or YAML parse. Parsed frontmatter is memoised by content digest.
   Tests: file reads and frontmatter parses per repeat append and per repeat inspect must not grow with
   items; a same-size in-place edit that restores mtime still changes the container hash; an
   out-of-band edit makes a guarded append return `STALE_RECORD`.
3. **Guard rounds** (`vault.py` `recheck_path_guards`, `_prepare_path_guards`, `batch_atomic_write`;
   `record_governance.py` `require_mutation_visibility(census=...)`). Each guard round proves every
   shared ancestor once; guards that need no parent creation are not re-captured; identical directory
   censuses handed in twice are proved once; pre-commit visibility reuses the snapshot's census when it
   covers every child directory. Marginal filesystem probes per extra item went from about 101 to 25
   (test asserts under 40).
4. **Manifest parse cache and one inspect snapshot** (`structured_collections.py`
   `parse_manifest_bytes`; `record_governance.py` `inspect_collection`, `records.py`
   `inspect_audit_gap`, `record_formats.py` `inspect_collection`). Manifest parses per append went from
   12 to at most 3; an inspect reads the collection once instead of three times. The audit-gap pass
   still learns whether the authorizer refused any path (`snapshot_denied`), so withheld items still make
   the chain incomplete rather than clean.
5. **Cheaper recheck, access memo** (`vault.py` `PathGuard.recheck`, `record_governance.py`
   `_access_refused`). Plain string joins and one access decision per path per pass. Measured effect at
   1,000 items: none beyond noise (625 ms against 628 ms p50). They are kept because they cost nothing
   and the profile showed the work, but they are not what moved the numbers.

## Is the target met?

**No.** The target was single-append p95 under 300 ms at 1,000 items with no O(N) stage per append.

- p95 at 1,000 items went from 5,078 ms to 706 ms (7.2 times), the guard refresh from 7,605 ms to 416 ms
  (18 times), and the round trip from 12.2 s to 1.04 s. At 100 items the append is 188 ms p50, 250 ms
  p95, which meets 300 ms.
- **What remains at 1,000 items (p50):** about 155 ms fixed, the same at 10 items: fan-out and index sync
  50 ms, manifest and collection resolution 15 ms, dispatcher 7 ms, and the rest small. On top of that
  about 470 ms grows with items, roughly 0.47 ms per item: the guard rechecks and directory censuses
  inside `batch_atomic_write` (111 ms at 10 items against 309 ms at 1,000, so +198 ms), snapshot assembly
  (+107 ms), visibility (+86 ms) and pre-commit authorization (+74 ms). Those four add to the 468 ms
  difference between the 10-item and 1,000-item totals.
- **Still O(N).** No stage is O(N) in bytes read or parsed any more: unchanged items are never re-read,
  re-hashed or re-parsed. But several stages still touch every item once or more per append in
  Python and in syscalls: a `lstat` per item per guard round (about nine rounds), a census scan per
  directory (about ten scans), and a per-path authorization decision. At 10,000 items (caps lifted) the
  append is 5.2 s p50, and the fan-out itself grows from 50 ms to 351 ms there, which I did not
  investigate.
- The product cap of 2,000 items per collection bounds the worst case: by this curve about 1.1 s p50 for
  an append and 0.9 s for a refresh at the cap.

Racy-window note for the cache: a file whose mtime or ctime is within two seconds of being read is never
trusted from the cache, so a back-to-back sequence re-reads only the last few files written. Stat
generations are not trusted on Windows, where `st_ctime` does not move on a write; there every read is
by content, as before.

# File design against SQLite-authoritative

## What was compared

1. **Index-backed file design.** What is built above (stat-generation cache, batched guards). The
   Markdown items stay the source of truth.
2. **Throwaway SQLite-authoritative prototype** (`scripts/prototype-sqlite-records.py`, not in `src/`,
   not wired to anything). Rows in a per-vault SQLite table, same natural key (unique index), the
   guard is a per-collection generation integer, one audit row per transition in the same transaction,
   Markdown item files rendered from the row by the real `render_markdown_item`. WAL mode,
   `synchronous=FULL` (each commit is durable) unless stated. Two projection modes: **sync**, the file is
   rendered and atomically written with `fsync` inside the transaction before commit; **async**, the
   transaction commits the row plus an outbox row and a worker thread renders and writes afterwards.

The prototype measures the storage layer only. It leaves out request validation, the writer lease,
the idempotency ledger, governance and the index fan-out. Those are size-independent and shared by both
designs. To compare like with like I add the measured fixed cost back: at 10 items the whole
file-design append is 157 ms, of which about 70 ms is item-dependent storage work (snapshot 5, visibility
3, pre-commit 2, the guard and write part of `batch_atomic_write` about 60), leaving **about 90 ms** of
shared cost. The composite below is that 90 ms plus the prototype number. It is an estimate; a real
implementation would carry costs I have not modelled.

## Numbers (p50 / p95, ms)

| Items | Design | Append (storage layer) | Append (composite est.) | Guard refresh | Projection lag |
|---:|---|---:|---:|---:|---:|
| 1,000 | File, index-backed | n/a | 625 / 706 | 416 / 546 | none |
| 1,000 | SQLite, projection in transaction | 2.6 / 3.5 | about 93 / about 100 | 0.02 / 0.03 | none |
| 1,000 | SQLite, async projection | 1.2 / 1.6 | about 91 / about 100 | 0.02 / 0.4 | 22 / 34 |
| 10,000 | File, index-backed (caps lifted) | n/a | 5,202 / 5,706 | 6,089 / 6,479 | none |
| 10,000 | SQLite, projection in transaction | 3.0 / 3.8 | about 93 / about 100 | 0.03 / 0.04 | none |
| 10,000 | SQLite, async projection | 1.6 / 2.8 | about 92 / about 100 | 0.04 / 0.11 | 4 / 6 |

With `synchronous=NORMAL` (commit not fsynced, durable to a process crash but not power loss) the sync
projection is 1.8 to 2.4 ms and the async foreground append about 0.1 ms; projection lag rises to 31 to
37 ms p50. The guard refresh in the composite would still pay the dispatcher and collection resolution
(about 15 to 25 ms), not 0.02 ms, because the client-visible refresh is a command.

Read this as: the SQLite design is flat in the number of items and the file design is linear, and the
gap is two orders of magnitude at 10,000 items. At 1,000 items the composite meets the 300 ms target with
a large margin; the file design does not. At 100 items or fewer the file design is already within the
target and the difference is small next to the fixed 90 to 150 ms, which neither design changes.

## Cost list for making SQLite authoritative

- **Migration of existing collections.** Every collection's item files must be parsed into rows,
  keeping each item's UUID (`exomem_id`), its natural key and body. The audit chain is currently split
  across the manifest's audit head, the activity log and archived logs; it would have to be converted to
  audit rows keeping transition ids and parent links so the existing gap and continuity checks still hold.
  Client-held `expected_container_hash` values change meaning. The manifest stays a Markdown file the
  user edits, so authority would be split between a file (schema) and a database (rows).
- **Backup and restore (restic and vault copy).** Today the vault directory is the whole truth, so a
  restic snapshot or a folder copy is a complete backup. With rows authoritative, the database must be
  in the vault (a live WAL database copied mid-write can be torn; it needs `VACUUM INTO` or a stop-the-
  writer snapshot before restic sees it) or in the machine-local state root, which is not part of the
  vault and is not backed up by copying it. Restore becomes: restore the database, then regenerate the
  projection. A vault copy alone would be a read-only mirror that cannot be written back to.
- **Obsidian visibility.** Items remain visible as files, but read-only in effect. An edit in Obsidian is
  not authoritative and would be overwritten by the next projection, so either it is refused with a
  visible notice, or an import path turns the diff into an audited row update. Sync tools (Obsidian Sync,
  Syncthing) work on the projection and cannot merge the database, so two machines writing produce a
  conflict the current file design resolves by ordinary file conflict handling.
- **Out-of-band edit handling.** Today: detected by the stat-generation cache and the container guard, and
  refused as stale. With SQLite: the edit lands in a file that is not authoritative, so it has to be
  detected on projection (hash mismatch against the row) and either reverted or ingested. That policy is a
  product decision the file design does not need.
- **Hosted cells.** A cell already has a single writer lease, so SQLite fits, and the cell volume holds
  the database. Cell export, restore and import currently operate on file trees and would need a database
  path; evidence bundles built from items would read rows.
- **Governance and withheld = absent.** Withheld items are absent because every read passes each item's
  path through the release filter. Rows do not remove that: each row keeps its projection path as the
  policy subject and the same filter applies before a query, an aggregate or an inspect returns
  anything, and audit rows need the same redaction. What changes is cost: evaluating policy per row on
  every query is O(N) unless the policy is compiled into a query predicate (path-prefix scopes map to
  `WHERE`; tombstones and access tiers need a join). Deletion and trash recovery today move files; they
  would move rows and re-project. Exactly-once still comes from the idempotency ledger, which is
  independent of the storage layer.
- **Everything else that reads items as pages.** `find`, the graph, lexical and embedding sync, plan
  links, due-state and capture sweeps read the Markdown files. With an async projection they see a write
  only after the worker runs (22 to 34 ms here; unbounded under a stalled worker), which breaks
  read-your-writes for the next `find`. A synchronous projection keeps that property and still costs only
  about 2 to 3 ms.

## Recommendation

**Do not make SQLite authoritative yet. Ship the index-backed file design, and build a derived SQLite
sidecar for the guard and natural-key lookup if collections are expected to pass about 1,000 items.**

- At the collection sizes people actually keep, the file design is fine after these fixes: 188 ms p50 and
  250 ms p95 at 100 items, and about 1.1 s at the 2,000-item cap. The 21-minute backfill was
  client-turn time, and bulk upsert (#1452) removes the per-row fixed cost that remains.
- The SQLite numbers are a large win only where the file design is linear, above roughly 1,000 items. The
  cost list is long, and the expensive items (backup and restore, Obsidian edits, migration of the audit
  chain, hosted export) are things that work today because the files are the truth.
- The middle path takes most of the win at a fraction of the cost: a per-collection SQLite sidecar
  in the existing derived-index location holding one row per item (key, natural key, item hash,
  generation) and a collection generation, rebuilt from the files and validated by the same stat
  generation. That gives an O(1) natural-key check and an O(1) container guard, keeps the files
  authoritative, keeps backups and Obsidian as they are, and can be thrown away and rebuilt. It does not
  remove the guard rounds inside the batch writer, so it would need the writer to trust the sidecar plus
  a per-write recheck of only the item being written.
- Revisit an authority switch if any of these become true: collections routinely exceed 2,000 items (the
  current cap), the workload needs multi-row transactions or bulk upserts as a first-class operation,
  or the fixed 90 to 150 ms is the complaint rather than the per-item growth. Agent traffic being over
  95 percent weakens the case for plain-file authority but does not by itself justify the migration
  and backup cost; what it does argue for is keeping the projection synchronous so agents keep
  read-your-writes.
