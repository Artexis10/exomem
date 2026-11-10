## 1. Phase 1: seamless switch, upgrade windows, release workflow and alerts (D1–D5, D8)

- [ ] 1.1 Substrate (separate repository): add the gateway hold and the upstream timeouts to the cloud handler (D5).
  - Buffer a body up to the cap, and stream a larger one without a hold.
  - Retry only on a connect error, at the interval with jitter, until the budget runs out. Then answer `503 CELL_NOT_READY` with `Retry-After`.
  - Count held requests against their own caps. Stop at once when the client leaves.
  - Add the connect timeout, and the upstream timeout that answers `504 CELL_TIMEOUT`.
  - Read every value from configuration. Refuse to start when one is missing. A budget of 0 turns the hold off.

  Verify with tests against a fake cell that refuses connections and then accepts them:
  - a request during a 5-second refusal gets the cell's answer, which catches a hold that still answers `503` at once;
  - a cell that closes the connection after the first request byte gets no retry, which catches a tool call that runs twice;
  - a body one byte over the cap is not held, which catches unbounded buffering in the gateway's memory;
  - a full per-tenant hold cap answers `503` with `Retry-After`, while another tenant's request proceeds, which catches one tenant's restart starving others;
  - a client abort stops the retries, which catches retries that outlive the client;
  - a cell that sends no headers gets `504 CELL_TIMEOUT` after the upstream timeout, and its slot frees, which catches the missing timeout;
  - a budget of 0 answers at once.
- [ ] 1.2 Render the gateway hold and timeout values from `cloudGateway.hold.*` and `cloudGateway.upstream.*` into the gateway's environment. Extend C3's pinned environment set and the chart schema. Verify: the chart test that pins the gateway environment passes with the new names. It catches a value that the chart never renders, which would stop the gateway at its next deploy.
- [ ] 1.3 Substrate (separate repository): narrow the deploy principal to `GET` and to `PUT` with `cellImage` alone. Add a receiver capability that the alert receiver accepts only for the job `cloud-deploy`, with no missed-run schedule. Verify with route tests:
  - the deploy token with `clearRolloutPause` gets a refusal, and the rollout row does not change, which catches automation that can clear a pause;
  - the deploy token with `cellId` and `cellDesiredImage` gets a refusal;
  - the admin token still clears a pause;
  - the receiver refuses the `cloud-deploy` capability for any other job name, which catches a CI capability that can raise or resolve other alerts.
- [ ] 1.4 Measure client tool-call timeouts against the hold budget. For each MCP client that Cloud supports, record how long it waits for a tool-call answer, and whether it retries a `503` with `Retry-After`. Use a disposable cell that refuses connections for 10, 30, 45 and 60 seconds. Verify: design.md records each client's timeout and retry behaviour, and the chosen budget sits below the shortest timeout. It catches a budget that clients abandon before the cell returns.
- [ ] 1.5 Add hourly traffic counts to the cloud cell, and the readiness `traffic` fields `hourly`, `inflight` and `last_request_age_seconds` (D4). Verify with tests:
  - the counts survive a restart on the same volume, which catches counts kept only in memory that reset at each upgrade;
  - an hour when the cell was down reads as not observed, which catches a down hour that looks quiet and draws a window onto it;
  - probe requests add nothing to a count;
  - a corrupt counts file starts new counts, and the cell serves, which catches a counter that blocks the start;
  - a readiness answer with 672 hours stays below 64 KiB, which catches a block that `cellctl`'s capped read cuts off;
  - `last_request_age_seconds` is null before the first request.
- [ ] 1.6 Add `infra/cellctl/src/cellctl/upgrade_window.py`, a pure module beside `rollout.py`, with its unit tests first (D4). Verify:
  - a history that is quietest at 04:00 UTC gives `4 history`;
  - coverage below the minimum gives the default hour with the source `default`, which catches a window computed from one day of data;
  - an hour with no observed seconds never wins, which catches a down hour chosen as the quietest;
  - tied hours follow the cell-ID hash and stay the same across recomputes, which catches every tied cell choosing hour 0;
  - the due rule: idle in the window is due; busy is due only in the force minutes; outside the window is not due; a window that ends at midnight works.
- [ ] 1.7 Wire the windows into `cellctl` (D4):
  - the daily recompute, and the `exomem.io/upgrade-window` and `exomem.io/upgrade-window-at` annotations;
  - the due set into `select_upgrade_candidate`, with the canary exempt;
  - render-digest re-applies under the due set;
  - per-node concurrency after the canary;
  - the chart values and their schema.

  Verify with reconcile tests:
  - a candidate outside its window starts no attempt, and one inside its window starts after its idle minutes;
  - the canary starts at once outside its window, which catches a windowed canary that holds the fleet for up to a day;
  - a digest change waits for the cell's window, which catches restarts that bypass windows;
  - with a concurrency of 1, two due cells on one node give one attempt, and a due cell on another node also starts, which catches a count that is fleet-wide or unbounded;
  - writing the annotations changes no pod template and no render digest, which catches a recompute that restarts every cell each day;
  - a missing chart value stops `cellctl` at start;
  - `cellctl.upgradeWindow.enabled: false` selects candidates as before this change.
- [ ] 1.8 Add the barrier file `infra/contracts/upgrade-barriers-v1.json`, its JSON schema and its CI check (D3).
  - Seed the 0.147.0 entry. Read the release history for earlier secret-registry changes, and add each as `operator-step`.
  - State the author rule in `CONTRIBUTING.md`, and add the entry to its review checklist.
  - Add `cellctl.forwardOnlyImages` and the forward-only failure path to `cellctl`.

  Verify:
  - the CI check fails on an entry whose runbook section does not exist, which catches a barrier with no step that a person can follow;
  - the CI check fails on an unknown scope or kind;
  - a reconcile test: a forward-only target that misses Ready pauses the rollout and keeps the cell stopped with `UPGRADE_FORWARD_ONLY`, and no restore Job runs. It catches a restore onto external state that the previous image cannot read.
- [ ] 1.9 Add the four rollout alerts and the `exomem.io/candidate-since` annotation to `cellctl` (D8). Add the three thresholds to the observability contract and the chart. Verify with tests:
  - each alert fires on its condition and resolves when the condition clears;
  - a newer target keeps the earlier `candidate-since` time, which catches a stall that hides because releases arrive every 8 hours;
  - a pass that could not observe a cell sends no transition, which catches a blind pass that resolves a firing alert;
  - `cellctl` refuses to start without a threshold.
- [ ] 1.10 Add `.github/workflows/cloud-deploy.yml` and the `cloud-deploy` environment (D1, D2, D3). Put the logic in a script with unit tests on recorded API answers: release lookup, attestation check, newer-release check, pause check, platform gate, barrier check, `PUT`, poll and alert transitions. Verify:
  - an unattested digest sets nothing, which catches a pushed image that reaches the fleet;
  - a rerun of an older release sets nothing, which catches a rerun that moves the fleet back;
  - a paused rollout sets nothing, and the run fails with the pause's code, which catches automation that resumes a paused fleet;
  - a chart change since the platform Deployment sets nothing and raises `cloud-release-platform-needed`;
  - a cell barrier with no crossing record sets nothing and raises `cloud-release-barrier`;
  - the `PUT` body holds only `cellImage`;
  - the CI log holds no token and no cell ID;
  - `actionlint` passes, and `gh api` shows that the environment's branch policy admits only `main`.
- [ ] 1.11 Update `docs/runbooks/cloud-platform-deploy.md` (D2, D3, D8):
  - record the platform Deployment after each manual upgrade, with one `gh api` command;
  - list `barriers_crossed` when an upgrade crosses a barrier;
  - read a paused rollout, and clear it with the admin token;
  - record the first platform Deployment and the first cell Deployment.

  Verify: every alert name of D2, D3 and D8 has a runbook step, and each new command runs as written against the rehearsal cluster. It catches an alert that fires with no step for the person who receives it.

## 2. Phase 1 rehearsal

- [ ] 2.1 With the operator's approval, rehearse a release end to end on the rehearsal cluster, with a disposable canary and two disposable cells. Verify:
  - the workflow sets the image, the canary rolls at once, and each other cell rolls only in its window, after its idle minutes;
  - a client that calls a tool every second through each switch sees no error within the budget, and the run records the waits;
  - a forced readiness failure pauses the rollout, `cloud-rollout-paused` fires, and the workflow fails;
  - nothing clears the pause until the admin token does.

  It catches a path whose parts pass alone, while the workflow, `cellctl` and the gateway disagree.
- [ ] 2.2 Measure each stop on real cells after phase 1: the stop, the pull, the init and the cold start, per cell. Verify: design.md replaces each "est." stop figure with the measured median and range, and names the run. It catches a "seamless" claim that rests on estimates.

## 3. Phase 2: restore point (D6)

- [ ] 3.1 Make a local cell's restore point a VolumeSnapshot taken after the stop. Start the target at once, and back up a clone of the snapshot after that. On failure, restore from a clone of the snapshot, or else from the attempt's off-site backup. Verify with reconcile tests and the K3s Live job:
  - the target applies before the backup Job starts, which catches a backup that is still inside the stop;
  - no snapshot is taken while a pod uses the volume, which catches a crash-consistent restore point;
  - a failed target restores from the snapshot, and no file that the target created remains;
  - a missing snapshot falls back to the off-site backup. With neither, the row records `RESTORE_FAILED`, and no image starts. It catches a guess onto an unrestored volume;
  - a `cellctl` restart at each step resumes from the annotations.
- [ ] 3.2 Drop `forget --prune` from the pre-upgrade backup of a cell without snapshot support, and keep its nightly prune. Verify: a test of `_backup_source` shows no retention for that cell's upgrade hold, and retention for its nightly backup. It catches a prune that holds restic's exclusive lock inside the stop.
- [ ] 3.3 Update `docs/runbooks/cloud-operator-restore.md` for the snapshot restore point. Verify: the runbook names `exomem.io/pre-upgrade-volume-snapshot` and the fallback order, so a person who restores by hand uses the attempt's own state.

## 4. Phase 3: platform upgrade command (D7)

- [ ] 4.1 Make `cellctl` refuse to start when a column that it reads or writes is missing, and expose the same check for the command. Verify: a test against a schema without one column stops the start and names the column. It catches a controller that acts on half a schema.
- [ ] 4.2 Add `infra/scripts/platform_upgrade.py` with the machine review, the upgrade, the verify step and the platform Deployment record. Verify with tests on recorded tool output:
  - each failing review step stops the command before any change;
  - a secret contract change with no barrier entry stops the command, which catches an operator-step release applied without its step;
  - a crossed one-way barrier removes `--atomic`, and every other upgrade keeps it, which catches a Helm rollback across a one-way change;
  - a crossed one-way `cell` entry adds its digest to `cellctl.forwardOnlyImages`;
  - a failed upgrade records `failure` and no `success`.
- [ ] 4.3 Replace the runbook's manual upgrade steps with the command, and keep the manual steps as the fallback. Verify: an upgrade with the command on the rehearsal cluster records the platform Deployment, and the `cloud-deploy` workflow then runs.

## 5. Later phase: deploy tier and prepare Job (D7, D9)

- [ ] 5.1 After `make-cloud-cells-operator-resistant` tasks 4.1 and 4.6, decide the custody of the CI path: an ephemeral private-network peer and a revocable ServiceAccount token for the deploy identity. Then add the path, so that a release that changes `infra/cellctl/**` and not `infra/helm/platform/**` rolls `cellctl` as an image-only change. Verify:
  - such a release rolls `cellctl` with no presence step, and records the platform Deployment;
  - an unattested image and a change to anything but an image are denied, which catches a CI path that can change more than images.
- [ ] 5.2 Add the pull-only prepare Job: one Job per node per target, before the node's first window. Verify: the first attempt on a node after the Job shows no image pull in its events, and the Job fits the namespace quota. It catches a prepare Job that runs on the wrong node.
- [ ] 5.3 Open a separate OpenSpec change for the catalogue warm-build. Verify: that change decides the cross-process adoption, the second pod's quota and the key handover for tenant-locked cells before any code. It catches a warm-build that ships without a key handover for locked cells.
