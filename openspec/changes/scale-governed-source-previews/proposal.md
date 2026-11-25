## Why

A large Source reclassification preview meets its load target on an ungoverned vault, but not on a governed one. With 10,000 governed referrers in one flat folder, each referrer costs about 20 ms of admission work. Most of that time goes to re-listing the folder to resolve one physical name. The background build needs about 209 s, so it stops at the 50 s request budget, keeps no progress and restarts with the same cost. Without that cap, it fails its final snapshot check, because index and governance generations move during the build. A governed owner therefore sees the preview warm indefinitely.

Release has a second gap. A referrer with a container parent or bridge dependency still rereads its bytes at the terminal gate, so a large dependent referrer can make a page slow or stale.

## What Changes

- Resolve a referrer's physical name without enumerating its folder once per referrer. Reuse one bounded listing of the folder while its identity is unchanged.
- Give the background preview build a budget and progress model of its own, so a long governed build finishes, or resumes, instead of restarting from nothing.
- Keep the final snapshot check exact, but make it fail only on changes that affect the report, and keep generations that the build itself moves from discarding it.
- Release a referrer with a container or bridge dependency from held evidence for its own bytes, plus current proof of each dependency.
- Return a typed public error for an interrupted or cancelled preview job, instead of its raw state.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `command-surface`: a governed reclassification preview meets the load target, and a dependent referrer is released without rereading its bytes.

## Impact

- `reserved_paths` physical-name resolution and `held_fs` listing.
- The background preview build in `reclassification_preview`, `reclassify_source` and `maintenance_jobs`.
- The terminal release in `governance/egress`, and container and bridge evidence.
- Follows `make-source-reclassification-reversible`, which ships the paged preview, the receipt reuse and the simple-row release.
