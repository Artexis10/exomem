## Why

Under a large import, most full lexical repair passes still end with
`source_changed` at the `materialize_replay` stage. The cause is a race on pages
that every governed write rewrites, such as an index or the activity log. The
write changes the page on disk before its event reaches the freshness registry:
the batch snapshot restores the page's timestamps, which moves its ctime, and
the rename lands before the publication. The detached replay then proves the
page against the older registry signature, fails, and discards the whole pass.
The registry observes the newer write moments later.

On 2026-10-31, a cloud rehearsal of a 69,300-file import recorded 38
`materialize_replay` declines against 10 publications. In a local reproduction,
each failed path was a hot page whose disk state the registry had not yet
observed, and the registry had moved past the replay's target within 2 s.

The same race then reaches the final source census. Its walk sees a hot page
or a new page of an import batch before the registry does. When the census
replay has no delta for that path, the census reports `source_changed`.

## What Changes

- A detached replay off the publication barrier applies its rows first and
  proves each path afterwards. When a path fails its proof, the replay waits a
  bounded time for the registry to observe a newer state of that path, applies
  the newer part of the delta and proves again. It retargets at most a fixed
  number of times in one replay.
- A path that the registry does not observe changing in time still declines
  the replay as `source_changed`. A path that changes again after each retarget
  also ends the pass. A replay under the publication barrier never waits.
- The census replay first waits, on the same retarget budget, for the
  registry to observe each path where the census walk and the registry
  disagree. It then replays and re-proves those paths before the census judges
  the walk. A path that the registry does not observe in time still declines
  the pass as `source_changed` at the census.
- `lexical_repair_finished` carries a `retargeted` count for full passes. It
  counts the waits of both the replays and the census.

## Capabilities

### Modified Capabilities

- `live-index-freshness`: a detached lexical replay and the final census wait
  for the registry to observe a governed write that is still publishing, and
  replay it instead of discarding the pass.

## Impact

- `src/exomem/lexstore.py`: detached replay retargeting, the census wait and
  the repair event field.
- `tests/test_lexstore_atomic_rebuild.py`: replay cases for a late event and a
  page that never stops changing, and a census case for an unpublished batch.
