# Proposal: fix-catalog-identity-on-windows

## Why

Catalog publication refuses on Windows. Every governed mutation that publishes a
v4 catalog generation raises

```
CatalogPublicationError: catalog content identity no longer matches the reviewed predecessor
```

from `governance/catalog_publication.py`, surfacing as `SemanticWriteError`
(`semantic_writes.py`) for a semantic write and `DeleteFileError`
(`delete_file.py`) for a removal. One root cause, thirty-two distinct failing
tests in `tests/test_governance_active_tuple.py` alone, plus related failures in
`test_graph_epoch_protocol.py` and `test_graph_lifecycle_windows.py`. Linux is
green throughout.

**It arrived with the v4 catalog wave.** Bisected against the nightly matrix: the
lane was clean at `542db604` and failing at `3e643525`, and between them sit
PRs #800-#818, the wave that moved semantic writes, trash, move, recovery,
companion backfill and media sidecars onto v4 catalog publication.

**It has been invisible because the lane that sees it is advisory.** Since
`e85a88c1 chore(ci): move expensive gates off every PR (#787)` the
cross-platform matrix runs nightly and manual only, with a skipped placeholder
on pull requests. Nothing merged red; the matrix simply never ran on those PRs,
which is what #787 intended. The required Windows coverage on a PR is the
focused native-NTFS contract, and that contract does not exercise catalog
publication.

Separately, the nightly signal was itself degraded: the lane's session cap fired
below real Windows runtime, so shards reported clean summaries as failures and
buried these among clocks. That is `bound-cross-platform-session`, and it is why
this was not noticed sooner.

## What Changes

- Catalog publication's predecessor-identity match holds on every platform the
  package declares, not only where the path text happens to round-trip.
- The failure is covered by a test that runs on the required PR gate, so this
  class of defect cannot again be visible only in an advisory nightly lane.

## Capabilities

### Modified Capabilities

- `governance-kernel` — the v4 tuple publication contract states that
  predecessor identity is matched platform-independently.

## Impact

- `src/exomem/governance/catalog_publication.py` — the identity lookup.
- Whatever produces `item_identity` upstream, which this change has not yet
  traced.
- `tests/` — a reproduction that does not require a Windows runner.

## Status

**This files the defect; it does not fix it.** The tasks are unticked. Confirming
a fix needs either a Windows host or a reproduction that forces the mismatch on
any platform, and inventing one is the first task rather than an assumption.
