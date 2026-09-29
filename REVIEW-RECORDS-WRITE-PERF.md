# Review: PR #1457 — perf: cut the cost of a records append and guard refresh

Reviewed head `3b2dfee` against `origin/main` `c44eaa0`. Question asked: did any cache or
batching change a governance or integrity answer? Short answer: not on any reachable
production path. One latent fail-open in a public helper should be closed before merge.

**Verdict: REQUEST_CHANGES** (one Medium, small fix; everything else holds).

## Findings

### Medium — census reuse trusts any census, including an empty one
`src/exomem/record_governance.py:2935`, `:2975`. `require_mutation_visibility(..., census=...)`
skips the fresh visibility scan whenever `_census_covers_every_directory(census)` is true.
That check only proves closure *within* the given set: `all()` over `()` is true, and nothing
asserts a census targets `manifest.storage.source`. Reproduced (probe
`test_empty_census_is_not_proof_of_visibility`, `test_census_of_another_directory_is_not_proof`):
with an item refused by access policy, `census=None` raises `COLLECTION_NOT_FOUND`;
`census=()` or a census of an unrelated directory returns cleanly — the refused item is never
checked. Today every caller passes `snapshot.directory_guards` read from the same manifest
(`records.py` append/replay/delete/lifecycle; the later write rechecks those censuses), so it is
not exploitable now. But it is a governance answer that depends on caller discipline.
Fix: require exactly one census whose `target == manifest.storage.source` and treat anything
else as "not covering"; add the two probes as regression tests.

### Low — Windows branch untested
`record_item_cache.py:81`, `vault.py:1262`. Nothing in the PR sets
`STAT_GENERATION_TRUSTED = False`, and the Windows CI job runs only the held-fs subset, not
records. Probe `test_untrusted_platform_reads_by_content` (monkeypatched flag) passes: no cache
entry, `content` leaf guard, same-size edit with restored mtime is caught on recheck and on
re-read. Add it to `test_record_write_scaling.py`.

### Info — questions that came back clean
- **Stale cache.** Generation includes `ctime`, which userspace can't set, so `touch -r` /
  mtime-preserving sync is caught (existing test plus probe
  `test_generation_guard_catches_same_size_restored_mtime`). A write inside the 2 s racy window
  is never cached (`test_recent_write_is_never_cached`). Residual assumption, worth a sentence in
  the module doc: a filesystem whose `ctime` does not advance on write (some FUSE/network
  mounts) would defeat it; that is the Windows case under another name.
- **Ancestor swap.** A cache hit returns the guard captured at insert time; its ancestors are
  rechecked at the end of every read. Replacing the item's parent with a symlink to the same
  inode fails `recheck_path_guards` (`test_ancestor_swap_invalidates_a_cached_guard`). Batched
  rechecks share a verified set only within one round; each round builds a fresh one.
- **`_prepare_path_guards` skip** (`vault.py:3990`). Guards without missing parents keep the
  caller's original capture and are rechecked again before each replace — stricter than
  re-capturing.
- **Authorization pass.** Scoped to the three call frames (`record_governance.py:971`); it is
  entered inside `precommit_authorize_mutation`, which runs under the mutation lease, and is
  reset on exit (`test_pass_scope_does_not_outlive_the_call`). Pre-PR read policy/tombstones
  per path in the same loop, also before the write, and the write itself never rechecked
  tombstones; the PR moves the read point to loop start. No new stale-decision window. Keyed
  by vault root, so it can't answer for another vault. `full_release_filter` now also freezes
  tombstones at construction (`:932`); every caller uses it immediately.
- **Memos.** `_classified_reserved` keys on `(relative, kb_dirname(), id(_REGISTRY))`;
  `classify_logical` is pure over those and `_REGISTRY` is an immutable tuple. The item cache is
  keyed by `(root, relative)` and validated against disk, so no cross-vault bytes. The
  frontmatter memo is content-addressed and shared across vaults; safe because callers copy via
  `_json_value`, but the returned dict is mutable — consider `MappingProxyType` to make "treat
  as read-only" enforced.
- **`snapshot_denied`.** A refused item still yields a non-complete audit status from
  `inspect_collection` (`test_refused_item_still_makes_inspect_audit_incomplete`); the read's
  refusals are captured before the shared snapshot is reused (`:1524`).

## Verification
- Probes: 8 tests, 2 failed (the Medium), 6 passed, on the PR head.
- Local suites (scaling, records, governance, egress, lifecycle, vault; 83 modules): RESULT_LINE
- `gh pr checks 1457` equivalent (GitHub API): 36 checks — all run jobs green (12 core
  shards, 4 harness shards, Windows held-fs, E2E, lint, OpenSpec, required gate); 9 skipped
  by path filter, including the cross-OS matrix.
