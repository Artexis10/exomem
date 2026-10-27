# Design: Scale Derived Imports and Widen Time Bases

## Context

A derived collection keeps its rows in the derived file. Each row has a public
`item_key`. Today the key is `uuid5(collection, serialized natural key)`, stored as
16 bytes through the `uuid16` identity codec. The derived table has `UNIQUE(item_key)`,
and every query index ends with `item_key` as its tiebreak. Random keys spread inserts
over every leaf of these indexes, so each row dirties one page per index.

## Decisions

### D1. Ordered item keys for new derived collections

New derived collections use the `ordered-v1` scheme. Existing collections keep
`hashed-v1`.

The key is an injective, order-preserving encoding of the natural key. It has no hash
tail. Each natural-key field becomes one component, in declared order:

| Part | Encoding |
|---|---|
| Presence | `0x01` for a value, `0x02` for null, so nulls sort last; null is not `0xFF`, because `0xFF` follows `0x00` inside a string and two natural keys could then encode alike |
| `date` | 24-bit big-endian offset-binary days since 1970-01-01 |
| `datetime` | 64-bit big-endian offset-binary UTC microseconds, then 2 bytes: the offset in minutes plus 32768 |
| `integer` | the FoundationDB tuple-layer integer encoding |
| `number` | an order-transformed IEEE 754 binary64, then `0x00` for a JSON float, or `0x01` and the integer encoding for a JSON integer, so 5 and 5.0 stay distinct |
| `boolean` | `0x00` or `0x01` |
| `string`, `enum`, `link` | NFC UTF-8, with each `0x00` written as `0x00 0xFF`, then a `0x00` terminator |

An incomplete natural key (the member-position fallback) encodes as `0xFE`, the first
16 bytes of the member's SHA-256, then the integer encoding of the row's position in
the member. `0xFE` sorts after every complete key.

A key longer than 320 bytes keeps its first 288 bytes and appends the 32-byte SHA-256
of the serialized natural key. The public spelling of 320 bytes is 512 base32hex
characters, which is `_MAX_ITEM_KEY_BYTES`.

The key is stored as raw bytes through a new identity codec, `ordered`. Its public
spelling is lowercase, unpadded RFC 4648 base32hex. The base32hex alphabet ascends in
ASCII, and the encoding is a big-endian bit stream, so text order equals byte order,
including for prefixes.

The store records the scheme in `collections.item_key_scheme` (`hashed-v1` or
`ordered-v1`, default `hashed-v1`). Two triggers guard it: one refuses any change, and
the other refuses `ordered-v1` unless `derived = 1`. Creation sets the scheme. The
layout's identity codec follows the scheme: `uuid16` for `hashed-v1`, `ordered` for
`ordered-v1`. One scheme-aware key function serves collapse, derived items, preview and
rebuild. Inspection, `describe` and preview report the scheme and the key byte lengths.

A natural key with a location-classified field gets `hashed-v1`, because an ordered key
would expose the location in the public key. Classifying a natural-key field of an
`ordered-v1` collection as location is refused, through a revise or through mapping
coverage.

Ties now sort by natural key. A row query with no `order_by` returns the lowest natural
keys first, and `describe` says so. The cursor format is unchanged.

Rejected alternatives:

- A UUIDv8 with an ordered prefix has too few ordered bits for string or wall-clock keys,
  and it needs a collision check.
- A random public key with an ordered surrogate key leaves about 5.4 KB per row through
  the unique index, or needs scans.
- A `row_id` tiebreak alone measured about 5.4 KB per row and changes tie order.
- Key text in the `utf8` codec costs about 2.3 GB more at 47 million rows.

### D2. Indexes and query compilation

Query indexes still end with `item_key`. Index validation and the typed index proof do
not change. Native SQL compilation changes only in `key()` handling for the `ordered`
codec:

- an equality literal that is not a valid key matches nothing;
- a range comparison with an invalid literal is refused with `QUERY_VALUE_INVALID`;
- a valid literal compares as bytes.

The derived table adds `CHECK(length(item_key) BETWEEN 1 AND 320)` for the `ordered`
codec.

### D3. A stop reverts only the open member

1. When a member opens, its progress record stores `row_floor`, the largest row id at
   that moment, and `restored: 0`.
2. On the first update of an earlier row (`row_id <= row_floor`) in this member, the
   apply step copies the row into `cq_<hex>_undo` with `INSERT OR IGNORE ... SELECT`, and
   counts the copies in `restored`.
3. The assessment gains `revert`. It applies when the job no longer holds the record,
   the record's sequence equals the applied sequence, and the record has `row_floor`.
4. Settle and step run the revert in bounded steps of `REBUILD_STEP_ROWS`:
   1. Restore the undo rows.
   2. Delete the rows above the floor, highest first.
   3. Restore each touched rollup bucket from the store: copy the store's bucket, or
      delete the bucket when the store has none. Set `derived_flagged` from the store.
   4. Delete the `rollup_members` above the floor, and place the restored rows again.
   5. Clear the undo table, release the record and mark the collection `ready`. The
      applied sequence does not change.
5. `QUERY_REBUILDING` progress names `reverting` and the rows that remain.

A full rebuild still runs when the record has no `row_floor` (an older release opened
the member), when the undo count differs from `restored`, when the contract changed,
when a rollup is not ready, and under the existing rules for a foreign store, another
layout, or a file behind or ahead of the log.

### D4. Pragmas

The derived connection sets `journal_size_limit` to 64 MiB. A larger `cache_size` and
`wal_autocheckpoint` are tested only as an ablation on the new build. They are kept only
if both repeats show at least 15% fewer bytes per row and query latency is no worse.

### D5. A closed time-basis grammar

Each new basis key extends the closed grammar that `BASIS_PATHS` declares. No branch
names a dataset.

1. `floating: true` places a row by local date and wall clock only. It excludes `zone`,
   `offset` and `offset_minutes`, and refuses `clock` and `fold`. A floating row gets
   `local_date` and `local_datetime`, and never an instant or an offset. A value that
   carries its own offset still resolves as an instant.
2. A `YYYY-MM-DDTHH:MM` value, with or without an offset, reads as second `:00`. The
   importer's instant and wall-clock readers change; `scalars.parse_instant` does not.
3. `milliseconds: <path>` reads a finite number. `duration: <path>` reads an ISO 8601
   duration: an optional `-`, weeks and days, then hours, minutes and seconds, with a
   fraction allowed on the last unit. Years and months are refused with
   `TIME_BASIS_INVALID`, because their length depends on the calendar. These keys are
   exclusive with `seconds` and `index`.
4. `mapping.time.local_datetime` names a declared string field. It receives the
   canonical wall clock `YYYY-MM-DDTHH:MM:SS[.ffffff]`.
5. `rows` may end at an object (`days[].summary`), and `rows: "$"` makes the root
   document one row. `$index` is refused when the row path crosses no array. Row bounds
   do not change.

Compilation refuses:

- a floating basis whose `instant` or `offset` target is required or part of the natural
  key;
- a `local_datetime` target that is not a string field;
- `floating` combined with a zone, an offset, `clock` or `fold`.

Preview counts rows by resolution kind (`instant`, `local`, `day`). `describe` says that
instant filters exclude floating rows. A canonical mapping adds the new keys only when
the mapping declares them, so existing mapping hashes do not change.

The duration reader is a format check over a restricted ISO 8601 profile. A regex fits
here because it checks the shape of a field, not its meaning. A library such as
`isodate` returns calendar durations and adds a dependency.

### D6. Migration and rollback

Store revision 14 adds `collections.item_key_scheme` with `hashed-v1` for every existing
row, and adds its triggers. Writer admission migrates the store, as for revisions 9-13.
The derived layout stays at revision 3, so nothing rebuilds on upgrade.

Older releases refuse revision 14 with `COLLECTION_STORE_SCHEMA_NEWER`, locally and on
Cloud cells. A rollback restores the automatic pre-upgrade backup, then re-imports the
collections created since from the kept export files.

A member that a previous release opened has no `row_floor`, so a stop falls back to a
full rebuild. A marker in store metadata or the manifest was rejected: older releases
ignore it and would write hashed keys into an ordered collection.

## Proof

- The write probe under `benchmarks/` imports 460,800 rows (160 members of 2,880 rows)
  with two repeats each, on the base and on the change, on one host. It reads
  `write_bytes` from `/proc`. It covers three key shapes: `(local_date, series, $index)`,
  `(series, floating local_datetime)` and `(session text, $index)`. The pass bar is at
  most 1.0 KB written per row over the whole import for every shape, with the last
  window within 20% of the first full window.
- The probe's cancel arm stops a second import after about 3,000 staged rows. Bytes and
  time scale with the staged rows, and the collection digest after the revert equals the
  digest before the job.
- One end-to-end fixture import uses every new time and row form and is checked against
  hand-computed answers. Its fixtures are not shaped like any one export.
- Tests, each for a failure that only it catches:
  1. Frozen vectors for `ordered-v1`, and one for `hashed-v1`.
  2. The restore case, parametrized by scheme.
  3. An upgrade from a revision-13 store: the collection gets `hashed-v1` and nothing
     rebuilds.
  4. The scheme stays fixed, and a location classification of a natural-key field is
     refused.
  5. A revert mid-member that updated earlier rows, including a restart in the middle of
     the revert and a restart that completes it.
  6. The new time-basis and row-path forms and their compile refusals.
  7. An existing mapping's canonical hash is unchanged.

## Revisit when

- Typical real keys exceed about 40 bytes: then encode string components as a hashed
  group code.
- The probe does not reach about 1 KB per row, flat: then diagnose again before release.
