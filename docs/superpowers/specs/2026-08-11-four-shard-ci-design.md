# Four-shard CI design

## Goal

Reduce pull-request and release CI from roughly 16–18 minutes to a measured target below five minutes without dropping tests or adding shared-process concurrency.

## Design

Run the lean suite on both supported Python boundaries (3.11 and 3.13), but split each boundary across four isolated GitHub-hosted runners. Use `pytest-split` with committed durations and the `least_duration` algorithm so every collected test belongs to exactly one shard and new tests receive the suite-average estimate until timings are refreshed. Keep the user-facing sample-vault smoke once per Python version rather than repeating it in every shard.

Split the current retrieval job into three independent jobs: live-vector retrieval quality, model-free retrieval latency, and semantic-write latency. This preserves the commands and thresholds while removing their serial dependency. Cancel superseded pull-request runs, but never cancel `main` pushes. Add one stable aggregate gate that fails unless every constituent CI job succeeds.

## Safety

Separate runners preserve filesystem, process, port, environment, and mutation-lock isolation. Do not use `pytest-xdist`, change production timeouts, weaken performance thresholds, remove Python-version coverage, or select tests from the changed-file diff.

The committed duration file is scheduling evidence only. It cannot deselect a test: pytest performs normal collection first, and `pytest-split` assigns unknown tests an average duration. Timing refreshes happen from a complete green CI run.

## Verification

- A workflow policy test pins the four-by-two shard matrix, unique timing artifacts, independent retrieval jobs, cancellation policy, and aggregate gate.
- Collection-only runs for all four groups prove their union equals the unsplit collection with no duplicates or omissions.
- GitHub Actions supplies the acceptance measurement. The PR is successful only if every shard and retrieval job passes and the aggregate gate is green; the observed wall time is reported from the real run.
