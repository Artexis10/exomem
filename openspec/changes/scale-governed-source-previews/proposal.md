## Why

A large Source reclassification preview can warm indefinitely on a governed vault. With 10,000 governed referrers in one flat folder, most admission time goes to listing the folder again to resolve each physical name. The background build exceeds the 50 s request budget and restarts with the same cost.

Release has a second gap. A referrer with a container parent can still require a full reread at the terminal gate, which can make a report page slow or stale.

## What Changes

- Resolve a referrer's physical name without enumerating its folder once per referrer. Reuse one bounded listing of the folder while its identity is unchanged.
- Give the background build its own bounded budget and keep the exact snapshot check.
- Release container members, extraction parts, and scene frames from held evidence for their own bytes and current proof of their dependencies.
- Return a typed public error for an interrupted or cancelled preview job, instead of its raw state.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `command-surface`: a governed reclassification preview meets the load target, and a dependent referrer is released without rereading its bytes.

## Impact

- `reserved_paths` physical-name resolution and `held_fs` listing.
- The background preview build in `reclassification_preview`, `reclassify_source` and `maintenance_jobs`.
- The terminal release in `governance/egress`, including container and media dependencies.
- Follows `make-source-reclassification-reversible`, which ships the paged preview, the receipt reuse and the simple-row release.
