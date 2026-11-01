## Why

A retained interpretation records the prior source, the reason, the actor and
the hashes of a change, but not the Exomem release that wrote it. When a
compiled page holds a poor interpretation, such as a generic relation where a
typed one was due, the owner cannot tell which release produced it.

## What Changes

- Each new compiled-history entry records `exomem_version`, the release of the
  server that planned the transition. The release travels in the transition
  token, so an exact retry after an upgrade re-plans identical bytes.
- History reads return the field with the revision.
- Existing entries keep their bytes, stay admissible and are not backfilled.
- The model that drove a write is not known to the server and is not recorded.

## Impact

- Affected code: `src/exomem/compiled_history.py` and
  `src/exomem/semantic_writes.py` (an optional `exomem_version` key in the edit
  transition token; a release older than this change refuses a token that
  carries it, as it refuses other added keys).
- Stored format: one additive field on new history entries; the entry version
  stays 1 because readers already ignore fields they do not use.
