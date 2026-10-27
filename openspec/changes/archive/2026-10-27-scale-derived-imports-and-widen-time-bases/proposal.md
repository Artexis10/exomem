# Scale Derived Imports and Widen Time Bases

## Why

A large derived import writes far more to disk than the rows it stores. A rehearsal of a
health-data export wrote 23.6 KB per row of about 190 bytes. At the full export's size,
about 47 million rows, one import would write about 1.1 TB to the disk.

The cause is the derived item key. Today a derived row's `item_key` is
`uuid5(collection, serialized natural key)`, so keys arrive in random order. The
`UNIQUE(item_key)` index and every query index, which ends with `item_key` as its
tiebreak, receive each row at a random leaf. Each row dirties one 4 KiB page per such
index. SQLite writes the page to the WAL and again at checkpoint. A probe measured
12.2-19.5 KB per row, rising with size. The same probe with keys that arrive in
ascending order measured 0.70 KB per row, flat.

A stopped import multiplies the cost. A cancel drops the collection's derived rows and
replays every logged member from the start, even when only the open member changed.

The importer also lacks time and row forms that common exports use. Exports often write
local wall-clock times with no zone, minute-precision times, increments in milliseconds
or ISO 8601 durations, a summary object per day instead of an array, or one row per
document. Today each of these needs a mapping workaround or cannot be imported.

## What Changes

- New derived collections use the `ordered-v1` item key scheme. The key is an injective,
  order-preserving byte encoding of the natural key, stored as raw bytes. Its public
  spelling is lowercase unpadded base32hex, so text order equals byte order.
- Existing collections keep the `hashed-v1` scheme (today's uuid5). The store records the
  scheme per collection. The scheme is set at creation and never changes.
- A natural key with a location-classified field uses `hashed-v1`. Classifying a
  natural-key field of an `ordered-v1` collection as location is refused.
- A row query with no `order_by` on an `ordered-v1` collection returns the lowest natural
  keys first. Ties in a sorted query break by natural key. The cursor format is unchanged.
- A stopped import reverts only its open member: it restores the rows that the member
  changed and deletes the rows that the member added. A full rebuild remains the fallback
  when the revert cannot be proven safe.
- The derived connection sets `journal_size_limit` to 64 MiB, so a checkpoint truncates a
  large WAL.
- Time bases gain five general forms, each declared in the mapping:
  - `floating: true` places a row by its local date and wall clock, with no instant;
  - a `YYYY-MM-DDTHH:MM` value reads as second `:00`;
  - `milliseconds: <path>` and `duration: <path>` increments, beside `seconds` and `index`;
  - `mapping.time.local_datetime` names a string field that receives the canonical wall
    clock;
  - a row path may end at an object, and `rows: "$"` makes the whole document one row.
- Store revision 14 adds the scheme column. Older releases refuse a revision-14 store
  with `COLLECTION_STORE_SCHEMA_NEWER`.

## Impact

- Affected capability: `structured-collections`.
- Stored data: store revision 14 adds `collections.item_key_scheme`. The derived layout
  stays at revision 3. Nothing rebuilds on upgrade.
- Compatibility: a rollback to an older release restores the store backup that the
  upgrade procedure takes with `exomem collections backup`, then re-imports the collections
  created since from the kept export files.
- Existing mappings keep their canonical hash. A mapping hash changes only when the
  mapping declares one of the new keys.
- Code: `structured_collections`, `collection_store` (importer, derived rows, native
  identity and tables, schema and migrations, rollups, import time) and `query_engine`
  (native SQL compilation and validation).
