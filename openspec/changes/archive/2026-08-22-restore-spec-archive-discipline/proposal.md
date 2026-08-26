## Why

Exomem's active OpenSpec backlog has become an unvalidated parallel contract: shipped requirements can remain absent from canonical specs, duplicate changes can evolve independently, and CI currently validates only the canonical half. The backlog has already hidden a hosted least-privilege requirement and admitted duplicate attention-queue requirements, so restoring archive discipline is a correctness and security repair rather than repository tidying.

## What Changes

- Merge and archive shipped changes in `created:` and dependency order, using code, tests, and merge evidence rather than task checkboxes alone; preserve every change artifact and reconcile stale `MODIFIED` deltas against the implemented current contract instead of dropping newer scenarios.
- Add a deterministic repository audit that reports task-complete changes left active and fails the required gate when the archive backlog reappears.
- Extend required CI validation from canonical specs alone to the complete active-change plus canonical-spec surface.
- Make archive closure an explicit repository delivery rule: an implemented OpenSpec change is synchronized and archived in the same delivery unless its remaining work is named and genuinely incomplete.

## Capabilities

### New Capabilities

- `openspec-record-discipline`: Defines complete-contract validation, archive-backlog detection, evidence-based archive closure, and preservation of change history.

### Modified Capabilities

None.

## Impact

- Affects `openspec/specs/` and `openspec/changes/archive/` through a one-time, order-sensitive canonical merge of shipped changes.
- Adds a small stdlib-only repository audit plus focused tests.
- Tightens `.github/workflows/ci.yml` and the repository agent instructions; no runtime API, vault format, package dependency, or user-facing Exomem behavior changes.
