## Context

The motivating input is a 243 MB zip with 2,192 JSON members (3.68 GB uncompressed, 44.6 million rows). The largest member is 75 MB of JSON. The owner's service sits behind a proxy edge that refuses any request body over 100 MB. These numbers are the design's scale target; nothing in this change is specific to one vendor's export.

## Decisions

### 1. Upload sessions speak tus 1.0

Sessions use the tus 1.0 core protocol with the `creation`, `expiration` and `termination` extensions, so any tus client works. Exomem implements the server on its own upload code instead of adding a tus server library: the maintained Python servers need a web framework Exomem does not use and do not verify a whole-file SHA-256.

- **Routes.** `POST /upload/sessions` creates a session. `HEAD`, `PATCH`, `DELETE` and `GET` act on `/upload/sessions/<id>`. `OPTIONS` answers on both. `GET` returns the session state and, once committed, the preservation receipt.
- **Authentication.** Creation takes the credentials `/upload` takes on that listener: the upload token on the public path, or a local token on local ingress. Creation returns a 256-bit session secret in the `Exomem-Upload-Secret` response header. Every later request presents it in the same request header. The server stores only its SHA-256. The secret never appears in a URL or a log.
- **Creation fields.** `Upload-Length` is required. `Upload-Metadata` carries `filename`, `sha256` (required, lowercase hex of the whole file), and optional `scope`, `category`, `raw_protection` and `archive` (`members`). The server validates scope and category at creation, the same way `/upload` does.
- **Limits.** Each `PATCH` carries at most 64 MiB. The declared length is capped by `EXOMEM_UPLOAD_SESSION_MAX_BYTES` (default 2 GiB). One credential binding holds at most 4 live sessions, and creation reserves the declared length against free disk space. A session expires 24 hours after its last accepted `PATCH`. A sweep removes expired sessions at creation and at service start.
- **Storage.** Each session is a `.part` file and a JSON record in a 0700 directory under the vault's state directory, the same shape as `held_uploads.py`. The server keeps the running SHA-256 in memory; after a restart it re-hashes the `.part` file once before it accepts more bytes.
- **Commit.** The final `PATCH` that reaches `Upload-Length` returns at once. A background task compares the SHA-256, then preserves through `preserve.preserve_stream`, or through member expansion when `archive=members`. The session record moves through `receiving`, `verifying`, `committing`, then `committed` or `failed` with a stable code. A SHA-256 mismatch fails the session and deletes the bytes. The `.part` file is deleted after the commit.
- **CLI.** `exomem attach` uses a session when the file exceeds the single-request cap on its listener, or with `--resumable`. It resumes with `HEAD` and records the session URL and secret in a 0600 file under the user's state directory, so a second run continues the same upload.

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

1. Validate the central directory before reading any member: at most 65,536 entries, each member at most 2 GiB uncompressed, and a declared uncompressed total within the free-space check. Refuse zip-slip, absolute and drive paths, symlink entries, duplicate normalised names, and encrypted entries. These checks move from `hosted_transfer_routes.py` into `archive_members.py`, which adoption staging also uses.
2. For each member: inflate it, hash it, gzip it into a temp file in the pool, fsync, and rename it into place only if no blob of that name exists. This step runs outside the vault mutation guard. `zipfile` stops at the declared size and checks the CRC.
3. Write the manifest and its companion in one `batch_atomic_write` under the guard. This is the commit point. A crash before it leaves blobs that no manifest names; the next expansion reuses them.

Preserving an archive whose SHA-256 a manifest in the same family already records returns `already_stored` and writes nothing.

### 3. Imports read members and nested documents

- **Source.** `source_ref` may name an export manifest. `members` then selects members by a glob over member paths (`fnmatch`; the caller supplies the pattern) or by a list of at most 256 paths. The job binding records the manifest's ref, SHA-256 and size, the selector, and the ordered list of selected member SHA-256s. Each member is read from its blob and hash-verified as it streams; a mismatch loses authority, as a changed source does today.
- **Checkpoint.** A member-source checkpoint is `{member, row}`: the member's index in the bound list and the count of rows taken from it. A restart re-reads that member and skips the rows already taken.
- **Format `json-document`.** The member is one JSON document. `rows` declares the row path, for example `devicePpiSamplesList[].ppiSamples[]`: each `[]` crosses an array. Field paths are relative to the row. `$.a.b` reads from the document root; `$.a[].b` reads from the enclosing element of that array. `$index` is the row's index in its innermost array, and `$value` is a scalar row's value. `{"const": v}` sets a literal. An ancestor field must appear before the row array in the document; one that appears after it is a row error that the preview reports. The reader is `ijson` with `use_float=True`, fed from the gzip stream, so a member never loads whole.
- **Re-import.** A store table `import_members` records `(collection_id, mapping_sha256, member_sha256, rows, state)`. A job skips members already imported into the same collection with the same mapping unless `reimport` is `all`. When two members carry the same natural key, the later member in path order wins, the same last-wins rule a single file uses.
- **Time bases.** A basis is a base, then a zone or an offset, then an optional increment.
  - Base: `{"date": path}` (local midnight) or `{"instant": path}`.
  - Zone or offset: `"zone": "<IANA name>"`, `"offset_minutes": path`, or `"offset": path` (ISO `±HH:MM`). An instant with its own offset needs neither. An unzoned instant without one is refused `TIME_BASIS_UNZONED`, as today.
  - Increment: `"seconds": path`, or `"index": "$index"` with `"every": {"s": n}` or `{"ms": n}` (a constant or a path).
  - `"clock": "elapsed" | "wall"` is required when a zone and an increment are both present. `elapsed` adds the increment to the UTC instant of local midnight; `wall` adds it to the local wall clock and then resolves the zone.
  - `"fold": "order" | "earlier" | "later"` resolves a repeated local hour. `order` takes the earlier offset until the wall clock steps backwards inside the repeated hour within one innermost array, then the later one.
  - A local time in a gap is refused `TIME_LOCAL_GAP` and counted. It does not stop the import.
  - Zone rules come from the pinned `tzdata` package through `zoneinfo`. The job binding records the `tzdata` version.
- **Scale.** A field may declare `"scale": n`, a finite number. The importer multiplies a numeric value by it before type checking.
- **Saved mappings.** A mapping may be saved in the collection manifest under `imports.<name>` through a governed revise, and `start` may name it. A renamed source field shows in preview as an absence count for that path.

### 4. Derived import collections (contract here, implementation in the replica batch)

A collection declared `derived: true` takes rows only from imports. It refuses row edits, view edit-back and row tools with `COLLECTION_DERIVED`. Its rows stay out of the vault replica. The replica carries its manifest, its saved mappings, its rollups and its import log. The log records, per import, the manifest ref and SHA-256, each member's SHA-256 and row count, the mapping SHA-256, and the importer and `tzdata` versions. After a loss, Exomem replays the logged imports from the preserved members and checks each member's row count against the log. Queries report the rebuild's progress and never report its rows as complete or zero before it ends. An original that a derived collection's log names cannot be pruned.

## Risks

- **Gzip CPU.** Recompressing 3.7 GB costs minutes on a small machine. If measurement shows it matters, copy each member's deflate stream into a gzip wrapper instead of recompressing.
- **Member stability across exports.** Deduplication across re-exports holds only when a vendor writes an unchanged older member byte for byte. If it does not, each export costs about one archive in blobs, and natural keys keep the imported rows correct.
- **Abandoned sessions** hold disk until their expiry. The per-binding limit and the space reservation bound this.
