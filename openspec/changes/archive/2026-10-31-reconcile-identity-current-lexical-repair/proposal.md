## Why

Every full lexical repair parses the whole corpus into a new catalogue. On a
vault of about 12,000 pages that pass takes 40 to 75 minutes, and recall stays
unavailable for all of it. A process that restarts with a lagging catalogue
cannot replay a delta, because the freshness registry's instance is
process-local, so it falls to the same full pass. The catalogue it discards is
often nearly current: most pages have not changed since it was built.

## What Changes

- Decision: a full lexical repair reconciles an identity-current catalogue by
  page content instead of re-parsing the corpus.
- When the live catalogue exists, is not retired, carries this release's schema
  and was built under the repair's captured catalogue identity, the repair
  copies it into the replacement through SQLite's online backup. It then
  re-parses only the pages whose content hash, mtime, scope membership or
  semantic generation differ from the walk, removes the pages the walk no
  longer holds, and inserts the new ones.
- A missing, retired or quarantine-bound catalogue, a schema or identity
  mismatch, a copy that fails SQLite's quick check, declarations that differ
  from a fresh build's, and a copy or reconcile error keep the whole-corpus
  build.
- Delta replay, checkpoint persistence, the WAL fold and guarded publication
  are unchanged.
- The repair logs one line per pass that names the route, `reconciled` or
  `rebuilt`, with page counts and the elapsed milliseconds.

## Capabilities

### Modified Capabilities

- `live-index-freshness`: adds "Full Lexical Repair Reuses An Identity-Current
  Catalogue".

## Impact

- `src/exomem/lexstore.py`: `rebuild_atomic` seeds its replacement from the live
  catalogue and reconciles it with a new `_reconcile_catalog` method. The
  standby's detached build still parses the whole corpus.
- `tests/test_lexstore_atomic_rebuild.py`: three tests for the reconciled and
  rebuilt routes.
