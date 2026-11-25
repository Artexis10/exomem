## Why

Cloud serves a paying tenant, and the launch never waited for five recovery drills. The drills were tasks 6.6–6.9 and 6.11 of `adopt-exomem-cloud-plain-cells`, which deferred them "until after launch" with no owner or end. Their open boxes kept that change from closing, though the change's own launch work does not depend on them.

The drills still matter. Each one proves a recovery path that the platform's runbooks already describe, and no recorded run shows that path works on the current deployment.

## What Changes

- Move the five drills, with their evidence limits, into this change: historical rollback, in-place restore after damage or a bad import, recovery after node loss, a server move, and memory and CPU admission.
- Add one contract: the platform claims a recovery path only after a recorded drill on the current deployment shows it works. Until then, the path is reported as unqualified.
- Leave `adopt-exomem-cloud-plain-cells` with only its launch work.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `cloud-platform-operations`: a recovery path counts as qualified only after a recorded drill on the current deployment.

## Impact

- `openspec/changes/adopt-exomem-cloud-plain-cells/tasks.md` loses tasks 6.6–6.9 and 6.11.
- No code changes. Each drill keeps its own authority boundary: node loss needs priced, approved disposable infrastructure, and a production resource change needs a separate operator go.
