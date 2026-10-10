## Context

The motivating input is an export of thousands of JSON members, several gigabytes uncompressed and tens of millions of rows, in a zip of a few hundred megabytes. Its largest member is tens of megabytes of JSON. The owner's service sits behind a proxy edge that refuses any request body over 100 MB. This scale is the design's target; nothing in this change is specific to one vendor's export.

## Decisions

### 1. Upload sessions speak tus 1.0

Sessions use the tus 1.0 core protocol with the `creation`, `expiration` and `termination` extensions, so a tus client works once it also sends the session secret that creation returns. Exomem implements the server on its own upload code instead of adding a tus server library: the maintained Python servers need a web framework Exomem does not use and do not verify a whole-file SHA-256.

- **Routes.** `POST /upload/sessions` creates a session. `HEAD`, `PATCH`, `DELETE` and `GET` act on `/upload/sessions/<id>`. `OPTIONS` answers on both. `GET` returns the session state and, once committed, the preservation receipt.
- **Authentication.** Creation takes the credentials `/upload` takes on that listener: the upload token on the public path, or a local token on local ingress. Creation returns a 256-bit session secret in the `Exomem-Upload-Secret` response header. Every later request presents it in the same request header. The server stores only its SHA-256. The secret never appears in a URL or a log.
- **Creation fields.** `Upload-Length` is required. `Upload-Metadata` carries `filename`, `sha256` (required, lowercase hex of the whole file), and optional `scope`, `category`, `raw_protection` and `archive` (`members`). The server validates scope, category and the filename at creation, the same way `/upload` does, and refuses `ARTIFACT_EXISTS` there when a single file's target is taken, so a bad name fails before any byte is sent. The commit checks again.
- **Limits.** Each `PATCH` carries at most 64 MiB. The declared length is capped by `EXOMEM_UPLOAD_SESSION_MAX_BYTES` (default 2 GiB). One credential binding holds at most 4 live sessions, and creation reserves the declared length against free disk space. A session expires 24 hours after its last accepted `PATCH`. A sweep removes expired sessions at creation and when the serving runtime starts. Creation holds a store-wide file lock, so the limit and the reservation hold across concurrent creates and processes.
- **Storage.** Each session is a `.part` file and a JSON record in a 0700 directory under the vault's state directory, the same shape as `held_uploads.py`. The server keeps the running SHA-256 in memory; after a restart it re-hashes the `.part` file once before it accepts more bytes.
- **Commit.** The final `PATCH` that reaches `Upload-Length` returns at once. A background task compares the SHA-256, then preserves through `preserve.preserve_stream`, or through member expansion when `archive=members`. The session record moves through `receiving`, `verifying`, `committing`, then `committed` or `failed` with a stable code. A SHA-256 mismatch, or content or a target that can never be preserved, fails the session and deletes the bytes. Any other failure, such as a full disk or a writer-lease handoff, keeps the bytes and moves the session to `retryable` with the code; the next request on the session retries the commit. The `.part` file is deleted after the commit.
- **Commit ownership.** A process commits a session only while it holds that session's file lock, so a second process can never commit it too. The commit is idempotent: a single file already at its target with the session's SHA-256 is `already_stored`, as a recorded archive is. Only the serving HTTP runtime's activation sweeps the store and resumes interrupted commits. A standby owns nothing until promotion, and a stdio server serves no session route.
- **CLI.** `exomem attach` uses a session when the file is larger than one part (64 MiB), when the listener refuses the file as too large, or with `--resumable`. The listener's cap is in the service's environment, so the CLI never reads it. It resumes with `HEAD` and records the session URL and secret in a 0600 file under the user's state directory, so a second run continues the same upload.

### 2. Archive members are the originals

`archive=members` on `/upload` or a session preserves a zip as its members. The zip's own bytes are not kept; its SHA-256 and size are recorded.

Layout under `Evidence/<scope>/<category>/`:

- `__exomem_raw_v1__<name>.export.json`: the manifest.
- `__exomem_raw_v1__<name>.export.json.md`: its companion, written by the ordinary sidecar path with `raw_protection.protect`.
- `__exomem_raw_v1__members/<first two hex>/<sha256>.gz`: one gzip blob per distinct member. The name is the SHA-256 of the uncompressed member bytes. The pool belongs to one Evidence family, the same scope as `destination_duplicate_index`.

The `__exomem_raw_v1__` prefix marks every path in the family as owner-only before its first byte is written. The vault stays plain files: no hardlinks or symlinks.

Manifest (`schema_version: 1`), a JSON document with sorted keys:

```json
{"schema_version": 1,
 "archive": {"filename": "...", "sha256": "...", "bytes": 0, "verified": "session|upload"},
 "members": [{"path": "dir/file.json", "sha256": "...", "bytes": 0, "modified": "ISO-8601 or null",
              "blob": "__exomem_raw_v1__members/ab/<sha256>.gz", "stored_bytes": 0}]}
```

Members are sorted by `path`. Directories are not members.

Write protocol:

1. Validate the central directory before reading any member. The end records (classic and ZIP64) must declare at most 65,536 entries and a directory of at most 16 MiB before `zipfile` loads it. Each member is at most 2 GiB uncompressed, and the largest one fits in the free space. Refuse zip-slip, absolute and drive paths, symlink entries, duplicate normalised names, and encrypted entries. These checks move from `hosted_transfer_routes.py` into `archive_members.py`, which adoption staging also uses.
2. For each member: inflate it, hash it, check that the free space holds a new blob, gzip it into a temp file in the pool, fsync, and rename it into place only if no blob of that name exists. This step runs outside the vault mutation guard. One expansion at a time runs in a vault, under a file lock, and it first removes the member temps that a killed expansion left in the pool. `zipfile` stops at the declared size and checks the CRC.
3. Write the manifest and its companion in one `batch_atomic_write` under the guard. This is the commit point. A crash before it leaves blobs that no manifest names; the next expansion reuses them.

Preserving an archive whose SHA-256 a manifest in the same family already records returns `already_stored`. It writes nothing, except that it restores any blob the manifest names that is missing from the pool, so re-attaching an export heals a lost member.

### 3. Imports read members and nested documents

- **Source.** `source_ref` may name an export manifest. Expansion and the importer read a manifest through one reader in `archive_members`. Before it parses anything, the reader requires the raw-protection prefix and the `.export.json` suffix in the name, a companion whose raw-protection binding names the file, and bytes within 32 MiB (512 bytes for each of the 65,536 entries) whose SHA-256 matches that binding. A principal that may read the manifest under raw protection may read the blobs it names; each batch re-proves that authority before it reads. `members` then selects members by a glob over member paths (`fnmatch`; the caller supplies the pattern) or by a list of at most 256 paths. The job binding records the manifest's ref, SHA-256 and size, the selector, and the ordered list of selected member SHA-256s. Each member is read from its blob only after one streamed pass proves its SHA-256 and size, because a batch commits rows before its member ends; a mismatch loses authority, as a changed source does today.
- **Checkpoint.** A member-source checkpoint is `{member, row}` (stored as `member` and `member_row` beside the job's row ordinal): the member's index in the bound list and the count of rows taken from it. A restart re-reads that member and skips the rows already taken. The open reader stays on the writer handle between batches, so each member is read once per host. A json-document file without members reads the same way, as one member. Consumed bytes count whole members.
- **Format `json-document`.** The member is one JSON document. `rows` declares the row path, for example `days[].samples[]`: each `[]` crosses an array, and the path ends with `[]`. Members may also hold ndjson, json-array or csv. Field paths are relative to the row. `$.a.b` reads from the document root; `$.a[].b` reads from the enclosing element of that array. `$index` is the row's index in its innermost array, and `$value` is a scalar row's value. `{"const": v}` sets a literal. An ancestor field must appear before the row array in the document; one that appears after it is a row error that the preview reports. The reader is `ijson` with `use_float=True`, fed from the gzip stream, so a member never loads whole. Its C parser refuses integers beyond int64, which real exports carry as 64-bit unsigned IDs, so a member it refuses is read again from the start with ijson's pure-Python parser before it counts as malformed.
- **Re-import.** The store table `import_members` is the append-only import log: one row per completed member, written once, with a sequence number, the transaction id, the job id, the member index and SHA-256, the manifest SHA-256, the mapping SHA-256, the accepted and rejected counts, a row digest, the collection's row count after the member, the importer version and, for a zoned mapping, the zone-rules version. A job skips a member that an earlier job's log row records for the same collection and mapping unless `reimport` is `all`; a member identical to an earlier member of the same job is read again, so the later path still wins.
  - A job's reader stops at each member's end, so one batch never holds rows of two members. The batch that leaves a member appends its log row and one `import_member` transition, which the row's transaction id names, in the transaction that advances the checkpoint past it.
  - The row count after a member is the previous log row's count plus the items created since that row's transaction. Items are never deleted and a new item's row id exceeds every earlier one, so the count reads only the newest rows and does not grow with the collection.
  - The open member's counts and digest ride in the job checkpoint, so a restart resumes them with the rows. A row that a later row of the same member superseded within a batch counts as accepted, so the log does not depend on where batches end.
  - The row digest starts as the SHA-256 of nothing. Each accepted row replaces it with the SHA-256 of `<digest>\0<item key>\0<payload hash>`, where the payload hash is `tokens.payload_hash` of the row the member supplies with an empty body. When two members carry the same natural key, the later member in path order wins, the same last-wins rule a single file uses. A skipped member is not read again, so across jobs the member read last wins, whatever its path.
- **Time bases.** A basis is a base, then a zone or an offset, then an optional increment.
  - Base: `{"date": path}` (local midnight) or `{"instant": path}`.
  - Zone or offset: `"zone": "<IANA name>"`, `"offset_minutes": path`, or `"offset": path` (ISO `±HH:MM`). An instant with its own offset needs neither. An unzoned instant without one is refused `TIME_BASIS_UNZONED`, as today.
  - Increment: `"seconds": path`, or `"index": "$index"` with `"every": {"s": n}` or `{"ms": n}` (a constant or a path).
  - A date base with an increment needs a zone or an offset.
  - `"clock": "elapsed" | "wall"` is required when a zone and an increment are both present, and refused otherwise. `elapsed` adds the increment to the UTC instant of local midnight; `wall` adds it to the local wall clock and then resolves the zone.
  - `"fold": "order" | "earlier" | "later"` is required with a zone and resolves a repeated local hour. `order` takes the earlier offset until the wall clock steps backwards inside the repeated hour within one innermost array, then the later one. A wall time outside a repeated hour starts the rule over, so a later year's fold or the next series in the same array begins on the earlier offset.
  - A local time in a gap is refused `TIME_LOCAL_GAP` and counted. It does not stop the import.
  - Zone rules come from the pinned `tzdata` package through `zoneinfo`. The job binding records the `tzdata` version.
- **Scale.** A field may declare `"scale": n`, a finite number. The importer multiplies a numeric value by it before type checking.
- **Saved mappings.** A mapping may be saved in the collection manifest under `imports.<name>` through a governed revise, and `start` may name it. A saved import is `{format, mapping, members?}`; every governed create and revise compiles it against the manifest. A renamed source field shows in preview's `mapping.absent` as a count of sampled rows without that path.

### 4. Derived import collections

A collection created with `derived: true` (summary mode only, never converted later) takes rows only from imports.

- **Storage.** Its rows live in `collections-derived.sqlite`, beside `collections.sqlite` in the state directory, in a lean current-state layout: one table per collection with the item key, row version and typed columns, plus its projection table and rollup mirrors. The file has no per-row versions, sources, audit effects or receipts, so tens of millions of rows fit the size target. SQLite cannot hold a foreign key or trigger across attached files, which is another reason the layout is self-contained. The file also keeps its own bookkeeping: the store identity it belongs to, each collection's state and applied log sequence, and the in-member progress record.
- **Readers.** Only query readers attach the file (`ATTACH ... mode=ro AS derived`). A per-collection row source replaces the hard-coded `items` and projection relations in the query engine. A query takes its whole row basis from the derived file and refuses when that basis lags the main store. Snapshot, backup and adoption readers never attach it, so the replica and portability export exclude it without other changes.
- **Write order.** The writer uses two connections, because SQLite does not commit atomically across attached files in WAL mode:
  1. Each batch is one transaction in the derived file: rows, projection rows, rollup mirrors and the progress record.
  2. At the end of each member, one main-store transaction appends the import log row, copies the touched rollup buckets, advances the job checkpoint and records one `import_member` transaction in the chain.
  3. One derived transaction then records the applied sequence and clears the progress record.
- **Recovery.** One `reconcile` step runs when the writer opens and after a takeover. A file from another store is discarded. A finished main commit is finalised. A member in progress resumes at its recorded row. A file behind the log replays the tail. Any other state drops the collection's rows and rebuilds from the first log entry.
- **Chain and audit.** Each member is one transaction whose receipt carries the job, member SHA-256, counts, row digest and row count. The row digest is a running SHA-256 over each row's item key and payload hash in member order. `verify_store_chain` is unchanged. There are no `audit_effects` rows; the import log row is the member-level effect.
- **Rebuild.** A rebuild replays the log member by member with the same apply function as a live import. It checks each member's counts and digest, then checks the derived rollup mirrors against the main store's buckets. A mismatch stops with `DERIVED_REBUILD_MISMATCH`, naming the member and the logged and running importer and `tzdata` versions. A missing blob stops with `DERIVED_SOURCE_MISSING` and retries when the blob returns. Row queries refuse with `QUERY_REBUILDING` and progress until the rebuild ends. Rollups and the summary page answer from the main store, and the summary page reads the row count from the log.
- **Refusals.** One gate in the writer refuses any row mutation of a derived collection with `COLLECTION_DERIVED`. A trigger forbids changing the `derived` flag, and a revise that would change it gets the existing "create a NEW collection" guidance. Held summary-page edits carry `COLLECTION_DERIVED`. Release is collection-uniform only.
- **Backup.** `exomem collections backup` excludes the derived file by default. With `--include-derived` it snapshots the derived file first and the main store second, so after a restore the main store can only be ahead, which replays a tail.
- **Compatibility.** Store schema revision 9 adds the import log and the `import_member` receipt; a later revision adds the flag and its trigger. Older readers refuse a newer store schema already. The derived file joins the existing `collection-store` reserved-path descriptor, so no state migration is needed.
- **Keys.** A collection without a natural key gets deterministic keys from the member SHA-256 and the row's position.

## Risks

- **Gzip CPU.** Recompressing several gigabytes costs minutes on a small machine. If measurement shows it matters, copy each member's deflate stream into a gzip wrapper instead of recompressing.
- **Member stability across exports.** Deduplication across re-exports holds only when a vendor writes an unchanged older member byte for byte. If it does not, each export costs about one archive in blobs, and natural keys keep the imported rows correct.
- **Abandoned sessions** hold disk until their expiry. The per-binding limit and the space reservation bound this.
