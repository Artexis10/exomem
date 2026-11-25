## Why

`adopt-exomem-cloud-plain-cells` deferred four recovery drills, tasks 6.6–6.9, "until after launch". Their open boxes kept that change from closing, though its launch work does not depend on them.

The drills still matter. Each one checks a recovery path on the current deployment, and no recorded run shows that path works there.

## What Changes

- Move the four drills, with their evidence limits, into this change: historical rollback, in-place restore after damage or a bad import, recovery after node loss, and a server move.
- Leave `adopt-exomem-cloud-plain-cells` without them. Its task 6.11, memory and CPU admission, is the same work as task 3.9 of `add-cloud-service-resource-policy`, so it leaves adopt for that task rather than for this change.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

None. The drills check existing recovery requirements in `cloud-platform-operations`; they add no contract.

## Impact

- `openspec/changes/adopt-exomem-cloud-plain-cells/tasks.md` loses tasks 6.6–6.9 and 6.11.
- No code changes. Node loss needs priced, approved disposable infrastructure before it can run.
