## 1. Phase 1: seamless switch, upgrade windows, release workflow and alerts (D1–D5, D8)

- [ ] 1.0 Read the matching pick-by-problem rows of the engineering primary-source roster for retry budgets, rollout windows and deploy custody. Cite them in design.md. Verify: design.md cites each row, or records a design change that a row forces. It catches an implementation that repeats a design flaw the roster names.
- [ ] 1.1 Substrate (separate repository): add the gateway hold and the upstream timeouts to the cloud handler (D5).
  - Reuse the prepared-body reader of `cloud-artifacts.ts` and its 1 MiB cap for every request while the hold budget is above 0. Stream a larger body without a hold.
  - Size the reader's buffer from `content-length` when the header is present, with the 1 MiB cap as the bound.
  - Retry only on a connect error, listed by its documented error codes, at the interval with jitter, until the budget runs out. Then answer `503 CELL_NOT_READY` with `Retry-After`.
  - Count held requests against their own caps. Take an in-flight slot when the cell accepts. Stop at once when the client leaves.
  - Add the connect timeout, the header timeout that answers `504 CELL_TIMEOUT`, and the idle bound on a streamed body.
  - Map `504 CELL_TIMEOUT` in Home to its cell-unavailable message.
  - Read every value from configuration. Refuse to start when one is missing. A budget of 0 turns the hold off.

  Verify with tests against a fake cell that refuses connections and then accepts them:
  - a request during a 5-second refusal gets the cell's answer, which catches a hold that still answers `503` at once;
  - a cell that closes the connection after the first request byte gets no retry, which catches a tool call that runs twice;
  - a body one byte over the cap is not held and is still forwarded whole, which catches unbounded buffering and a truncated body;
  - a 1 KiB body with its `content-length` gets a buffer of that size, not 1 MiB, which catches a full-cap allocation on every request;
  - a full per-tenant hold cap answers `503` with `Retry-After`, while another tenant's request proceeds, which catches one tenant's restart starving others;
  - an accepted held request with no free in-flight slot gets the in-flight cap's answer and frees its hold slot, which catches a hold slot that leaks;
  - a client abort stops the retries, which catches retries that outlive the client;
  - a cell that sends no headers gets `504 CELL_TIMEOUT` after the header timeout, and its slot frees, which catches the missing timeout;
  - a cell that sends SSE headers and then stalls has its stream closed after the idle bound, and its slot frees, which catches a hung cell that holds a slot for ever;
  - a budget of 0 answers at once, which catches a hold that the rollback setting cannot turn off.
- [ ] 1.2 Render the gateway hold and timeout values from `cloudGateway.hold.*` and `cloudGateway.upstream.*` into the gateway's environment. Extend C3's pinned environment set and the chart schema. Verify: the chart test that pins the gateway environment passes with the new names. It catches a value that the chart never renders, which would stop the gateway at its next deploy.
- [ ] 1.3 Substrate (separate repository): add the image history, the rollout view and the narrower deploy principal (D1).
  - Record each digest the first time it becomes `cell_image`, with its release tag and the time. Require the tag for a digest new to the history.
  - Add an idempotent seed: every published release's cell image digest and tag, the live `cell_image` and `last_good_image`, and each cell's observed and desired images. A digest that matches no published release gets no tag.
  - Grant `cellctl`'s database role read access to the history table.
  - Refuse a deploy-token `PUT` whose digest the history holds, with `CELL_IMAGE_SEEN`.
  - Add `GET /api/exomem/admin/cloud-release/rollout`: `cell_image` and its tag, the pause and its code, `canary_on_target` from the canary row's observed image and readiness, and counts. No cell or tenant ID.
  - Narrow the deploy principal to the rollout view, and to `PUT` with `cellImage` and `release` alone.
  - Add a receiver capability that the alert receiver accepts only for the job `cloud-deploy`, with no missed-run schedule.

  Verify with route tests:
  - the deploy token with a digest that was `cell_image` before gets `CELL_IMAGE_SEEN`, and `cell_image` does not change, which catches a merge that re-deploys a withdrawn release;
  - after the seed, a deploy-token `PUT` with a release digest published before the history shipped gets `CELL_IMAGE_SEEN`, which catches a workflow bug that picks an older release on ship day;
  - a second seed run adds no row and changes no tag, which catches a re-seed at migration step 6 that rewrites history;
  - `cellctl`'s role can read the history table and cannot write it, which catches a forward-only rule that cannot map any image;
  - the admin token can set an earlier digest, which catches a history that blocks a person's withdrawal;
  - the deploy token with `clearRolloutPause` gets a refusal, and the rollout row does not change, which catches automation that can clear a pause;
  - the deploy token with `cellId` and `cellDesiredImage` gets a refusal, which catches automation that can pin one tenant's image;
  - the deploy token on the full release view gets a refusal, which catches CI that can read every tenant ID;
  - the rollout view of a fleet with a new cell Ready on the target and the canary behind reports `canary_on_target` false, which catches a canary signal read from `last_good_image`;
  - the rollout view's answer holds no cell ID and no tenant ID, and keeps its size as the fleet grows, which catches a view that leaks identifiers or grows unbounded;
  - the admin token still clears a pause, which catches a narrowing that locks out the person;
  - the receiver refuses the `cloud-deploy` capability for any other job name, which catches a CI capability that can raise or resolve other alerts.
- [ ] 1.4 Measure client tool-call timeouts against the hold budget. For each MCP client that Cloud supports, record how long it waits for a tool-call answer, and whether it retries a `503` with `Retry-After`. Use a disposable cell that refuses connections for 10, 30, 45 and 60 seconds. Record the longest tool call that the rehearsal cell answers. Verify: design.md records each client's timeout and retry behaviour, a gateway latency budget, and the idle bound. The chosen budget sits below the shortest timeout, and the idle bound above the longest tool call. It catches a budget that clients abandon before the cell returns, and an idle bound that cuts a long tool call.
- [ ] 1.5 Add hourly traffic counts to the cloud cell (D4).
  - Keep the counts file under `/data/host`, outside `/data/vault`.
  - Add `inflight` and `last_request_age_seconds` to the readiness `traffic` block.
  - Serve the history on `/health/traffic-history`, with `retention_hours` and two integer arrays. Add the path to the shared protocol module, `infra/cellctl/src/cellctl/cell_readiness.py`, and update its note on the health-only listener.
  - Checkpoint the SQLite databases and sync at the stop signal (D6).

  Verify with tests:
  - the counts survive a restart on the same volume, which catches counts kept only in memory that reset at each upgrade;
  - an hour when the cell was down reads as not observed, which catches a down hour that looks quiet and draws a window onto it;
  - probe requests add nothing to a count, which catches a window computed from the kubelet's probes;
  - a corrupt counts file starts new counts, and the cell serves, which catches a counter that blocks the start;
  - a history answer with 672 hours stays below 64 KiB, which catches an answer that `cellctl`'s capped read cuts off;
  - the readiness answer carries no history, which catches history sent to every 5-second probe;
  - `last_request_age_seconds` is null before the first request, which catches a fresh cell that reads as idle for years and rolls at once;
  - the counts file never appears in a vault listing or a vault export, which catches platform data that leaks into the tenant's notes.
- [ ] 1.6 Add `infra/cellctl/src/cellctl/upgrade_window.py`, a pure module beside `rollout.py`, with its unit tests first (D4). Verify:
  - a history that is quietest at 04:00 UTC gives `4 history`, which catches a window that picks the busiest hour;
  - coverage below the minimum gives a default hour with the source `default`, which catches a window computed from one day of data;
  - 1,000 unmeasured cell IDs spread across the default range, and none lands in the nightly backup window, which catches a fleet-wide default hour;
  - three fixed cell IDs give fixed expected default hours from SHA-256 of the cell ID, in a fresh process each, which catches a per-process `hash()` that moves windows at every restart;
  - a default range that overlaps the backup window is refused, which catches a configuration that rolls cells during stopped backups;
  - an hour with no observed seconds never wins, which catches a down hour chosen as the quietest;
  - tied hours follow the cell-ID hash and stay the same across recomputes, which catches every tied cell choosing hour 0;
  - a lookback longer than the cell's reported retention is clamped, which catches a coverage computed over hours the cell never kept;
  - the due rule: idle in the window is due; busy is due only in the force minutes; outside the window is not due; a window that ends at midnight works. It catches a busy cell that never rolls, or one that rolls mid-burst.
- [ ] 1.7 Pin attempts and abort them on a pause in `cellctl` (D4).
  - Write `exomem.io/upgrade-target` when an attempt starts, and read only the pin in `_continue_upgrade`.
  - On a pause, end every attempt that has not applied its pinned target, and restart that cell on its current image.
  - Apply no target while paused.
  - Keep the first `held_cell_id` on a later concurrent failure.
  - Add `exomem.io/candidate-since`, set when a cell first becomes a candidate and kept on a newer target.
  - Copy `upgrade-target`, `candidate-since` and the window annotations from the live object into every StatefulSet render, outside the render digest.

  Verify with reconcile tests:
  - `cell_image` changes while an attempt waits for Ready, and the attempt keeps its pinned target, which catches an un-canaried image applied to an in-flight cell;
  - a pause while another attempt has stopped its cell but not applied its target restarts that cell on its current image, which catches a paused rollout that keeps applying targets;
  - an attempt that applied its target before the pause still restores on a miss, which catches a cell left on an unready image;
  - two concurrent failures keep the first `held_cell_id`, which catches a held cell that a person never sees;
  - a routine apply keeps every copied annotation, and the next pass recomputes no window, which catches server-side apply stripping annotations each pass;
  - a `cellctl` restart mid-attempt resumes with the pinned target, which catches a pin kept only in memory.
- [ ] 1.8 Wire the windows into `cellctl` (D4):
  - the daily recompute from `/health/traffic-history`, and the `exomem.io/upgrade-window` and `exomem.io/upgrade-window-at` annotations;
  - the due set into `select_upgrade_candidate`, ordered by `candidate-since`, with the canary exempt;
  - render-digest re-applies under the due set;
  - the chart values and their schema, with one attempt fleet-wide.

  Verify with reconcile tests:
  - a candidate outside its window starts no attempt, and one inside its window starts after its idle minutes, which catches a window that the selector ignores;
  - the canary starts at once outside its window, which catches a windowed canary that holds the fleet for up to a day;
  - a digest change waits for the cell's window, which catches restarts that bypass windows;
  - three releases arrive across three days, cells on one slot share a window, and every due cell upgrades within three windows, which catches a due order that starves the same cells each day;
  - writing the annotations changes no pod template and no render digest, which catches a recompute that restarts every cell each day;
  - an unreadable history keeps the cached window, which catches a window that jumps to the default on one failed read;
  - a missing chart value stops `cellctl` at start, which catches a deploy that silently runs without windows;
  - `cellctl.upgradeWindow.enabled: false` selects candidates as before this change, which catches a rollback switch that does nothing.
- [ ] 1.9 After task 1.7 ships, add per-node concurrency after the canary (D4). Keep the fleet-wide block on any restore hold. Verify with reconcile tests:
  - with a concurrency of 1, two due cells on one node give one attempt, and a due cell on another node also starts, which catches a count that is fleet-wide or unbounded;
  - while the canary's image differs from its target (`desired_image` or `cell_image`), no other attempt starts on any node, which catches an image that reaches tenants before the canary;
  - a canary that is behind its target but not eligible (stopped, not Ready, refused or in backoff) blocks every tenant attempt, including a tenant with an older `candidate-since`, which catches a tenant that pins a new image before the canary ran it;
  - a canary whose attempt ended on an older pin, while `cell_image` moved on, blocks every tenant attempt until it runs the new target, which catches a finished pin read as "canary on target";
  - a restore hold on one node blocks new attempts on every node, which catches a silent relaxation of today's restore rule.
- [ ] 1.10 Add the barrier file `infra/contracts/upgrade-barriers-v1.json`, its JSON schema, its CI check and the chart's copy (D3).
  - Seed the 0.147.0 entry. Read the release history for earlier secret-registry changes, and add each as `operator-step`.
  - State the author rule in `CONTRIBUTING.md`, and add the entry to its review checklist.
  - Add the forward-only rule to `cellctl`: decide a crossing attempt from the previous image's and the pinned target's releases, through the image history and the chart's barrier file.
  - Add the `forward-only-held` phase. `cellctl` never re-applies a target in it, and keeps the attempt's restore point. A person's clear of the pause ends it, and `cellctl` deletes the held snapshot and clone before the new attempt.
  - Count an unmapped previous image as older than every entry, and an unmapped target as newer.
  - Refuse an attempt whose target maps to a release before a one-way `cell` entry that the previous image is past. Pause the rollout with `UPGRADE_BACKWARD_CROSSING`, and keep the cell on its current image.

  Verify:
  - the CI check fails on an entry whose runbook section does not exist, which catches a barrier with no step that a person can follow;
  - the CI check fails on an unknown scope or kind, which catches an entry that the workflow and `cellctl` would skip;
  - the chart test fails when the chart's copy differs from the source, which catches a barrier that `cellctl` never sees;
  - a reconcile test: a cell on a pre-barrier image whose target is two releases past a one-way `cell` entry misses Ready. The rollout pauses, the cell stays stopped with `UPGRADE_FORWARD_ONLY`, and no restore Job runs. It catches a restore onto external state that the previous image cannot read;
  - a reconcile test: a previous image with no history row counts as pre-barrier, which catches an unknown image that restores across a barrier;
  - a reconcile test: an unmapped per-cell `desired_image` on a cell past a one-way entry is a crossing attempt, and a miss holds, which catches an unmapped target that restores across a barrier;
  - a reconcile test: `cell_image` set back to a release before a one-way entry that a cell is past starts no attempt, keeps the cell serving, and pauses with `UPGRADE_BACKWARD_CROSSING`, which catches a withdrawal that moves cells back across a one-way change;
  - a reconcile test: the forward-only hold keeps the attempt's restore point while it lasts, which catches a hold that leaves a person no state to restore by hand;
  - a reconcile test: passes after the forward-only miss apply no target and keep 0 replicas, which catches a hold that restarts the failed target every deadline;
  - a reconcile test: after a person clears the pause, one new attempt starts toward the current target, which catches a hold that no action can end.
- [ ] 1.11 Add the four rollout alerts to `cellctl` (D8). Add the three thresholds to the observability contract and the chart. Verify with tests:
  - each alert fires on its condition and resolves when the condition clears, which catches an alert that never fires or never resolves;
  - `UPGRADE_FORWARD_ONLY` fires `cloud-rollout-paused`, which catches a forward-only hold that nobody hears about;
  - a newer target keeps the earlier `candidate-since` time, which catches a stall that hides because releases arrive every 8 hours;
  - a pass that could not observe a cell sends no transition, which catches a blind pass that resolves a firing alert;
  - `cellctl` refuses to start without a threshold, which catches a deploy whose alerts are silently off.
- [ ] 1.12 Add `.github/workflows/cloud-deploy.yml` and the `cloud-deploy` environment (D1, D2, D3). Put the logic in a script with unit tests on recorded API answers: release lookup, attestation check, rollout view, pause check, platform gate, barrier check, `PUT`, poll and alert transitions. Keep the gate's globs in one configuration file. Verify:
  - an unattested digest sets nothing, which catches a pushed image that reaches the fleet;
  - a `CELL_IMAGE_SEEN` answer ends the run as success with nothing set, which catches a red run on every merge after a withdrawal;
  - a paused rollout sets nothing, and the run fails with the pause's code, which catches automation that resumes a paused fleet;
  - a chart template change since the platform Deployment sets nothing and raises `cloud-release-platform-needed`, which catches a cell image on a platform without its chart;
  - a change only to `infra/cellctl/tests/**` and Markdown files passes the gate, which catches a test-only release that waits for a person;
  - an uncrossed barrier entry of either scope sets nothing and raises `cloud-release-barrier`, which catches a one-way change that the paths miss;
  - the `PUT` body holds only `cellImage` and `release`, which catches a workflow that sends a pause clear or a cell pin;
  - a poll answer with `canary_on_target` false keeps polling, which catches a run that reports success before the canary is Ready;
  - the CI log of every recorded case holds no token, no cell ID and no tenant ID, which catches tenant identifiers in a public log;
  - a release run on `main` without a tag starts no run, which catches a run on every merge;
  - `actionlint` passes, and `gh api` shows that the environment's branch policy admits only `main`, which catches a token that a branch can reach.
- [ ] 1.13 Update `docs/runbooks/cloud-platform-deploy.md` and the data list (D2, D3, D4, D8):
  - record the platform Deployment after each manual upgrade, with one `gh api` command, then dispatch `gh workflow run cloud-deploy.yml --ref main`;
  - record the first platform Deployment, with every barrier entry at or below the live release in `barriers_crossed`;
  - list `barriers_crossed` when an upgrade crosses a barrier;
  - read a paused rollout, clear it with the admin token, and end a forward-only hold;
  - forbid a withdrawal across a one-way `cell` barrier, say to fix forward, and explain `UPGRADE_BACKWARD_CROSSING`;
  - for a key rotation, wait for every cell's render digest before the retire step, and name the bypass for an urgent re-render;
  - add the derived quiet hour per tenant to the list of data that the platform keeps, in Substrate's Cloud privacy documentation.

  Verify: every alert name of D2, D3 and D8 has a runbook step, and each new command runs as written against the rehearsal cluster. It catches an alert that fires with no step for the person who receives it, and a dispatch that the branch policy refuses.

## 2. Phase 1 rehearsal

- [ ] 2.1 With the operator's approval, rehearse a release end to end on the rehearsal cluster, with a disposable canary and two disposable cells. Start from a real tag publish, not a manual dispatch. If the tag run's completion starts no `cloud-deploy` run, add an explicit `gh workflow run cloud-deploy.yml --ref main` step to the release workflow's tag run, and repeat. Verify:
  - a real tag publish starts `cloud-deploy`, which catches a `workflow_run` trigger that a run started with `github.token` never fires;
  - the workflow sets the image, the canary rolls at once, and each other cell rolls only in its window, after its idle minutes;
  - a client that calls a tool every second through each switch sees no error within the budget, and the run records the waits;
  - a second `cell_image` during a disposable cell's attempt leaves that attempt on its pinned target;
  - a forced readiness failure pauses the rollout, `cloud-rollout-paused` fires, and the workflow fails with the code alone;
  - nothing clears the pause until the admin token does.

  It catches a path whose parts pass alone, while the workflow, `cellctl` and the gateway disagree.
- [ ] 2.2 Measure each stop on real cells after phase 1: the stop, the pull, the init and the cold start, per cell. Verify: design.md replaces each "est." stop figure with the measured median and range, and names the run. It catches a "seamless" claim that rests on estimates.

## 3. Phase 2: restore point (D6)

- [ ] 3.1 Make a local cell's restore point a VolumeSnapshot taken after the stop. Start the target at once, and back up one clone of the snapshot after that. Keep the clone and the upgrade hold until the backup Job ends. On failure, restore from that clone, or else from the attempt's off-site backup. Verify with reconcile tests and the K3s Live job:
  - the target applies before the backup Job starts, which catches a backup that is still inside the stop;
  - no snapshot is taken while a pod uses the volume, which catches a crash-consistent restore point;
  - on the K3s Live job, a write acknowledged just before the stop is present after a forced-failure restore. It catches a snapshot of an unflushed volume that loses the last commits;
  - a failed target restores from the snapshot, and no file that the target created remains, which catches a restore that leaves the target's migrations behind;
  - a restore during the backup Job reuses the attempt's clone, and the cell never holds two clones, which catches a restore that the quota refuses;
  - an hourly backup hold does not start while the upgrade hold keeps its clone, which catches a second clone request;
  - a missing snapshot falls back to the off-site backup. With neither, the row records `RESTORE_FAILED`, and no image starts. It catches a guess onto an unrestored volume;
  - a `cellctl` restart at each step resumes from the annotations, which catches an attempt that loses its restore point on restart.
- [ ] 3.2 Drop `forget --prune` from the pre-upgrade backup of a cell without snapshot support, and keep its nightly prune. Verify: a test of `_backup_source` shows no retention for that cell's upgrade hold, and retention for its nightly backup. It catches a prune that holds restic's exclusive lock inside the stop.
- [ ] 3.3 Update `docs/runbooks/cloud-operator-restore.md` for the snapshot restore point. Verify: the runbook names `exomem.io/pre-upgrade-volume-snapshot`, the fallback order, and the forward-only hold's kept snapshot and clone. It catches a person who restores by hand from `last_backup_snapshot` instead of the attempt's own state.

## 4. Phase 3: platform upgrade command (D7)

- [ ] 4.1 Make `cellctl` refuse to start when a table or column that it reads or writes is missing or unreadable, the image history included. Expose the same check for the command. Verify: a test against a schema without one column, and one without the read grant on the history table, stops the start and names the gap. It catches a controller that acts on half a schema.
- [ ] 4.2 Add `infra/scripts/platform_upgrade.py` with the machine review, the upgrade, the verify step, the platform Deployment record and the dispatch. Verify with tests on recorded tool output:
  - each failing review step stops the command before any change, which catches an upgrade that runs past a failed check;
  - a secret contract change with no barrier entry stops the command, which catches an operator-step release applied without its step;
  - a crossed one-way barrier removes `--atomic`, and every other upgrade keeps it, which catches a Helm rollback across a one-way change;
  - a success dispatches `cloud-deploy` with `--ref main`, which catches a platform upgrade after which cells never move;
  - a failed upgrade records `failure`, no `success` and no dispatch, which catches cells that move onto a failed platform.
- [ ] 4.3 Replace the runbook's manual upgrade steps with the command, and keep the manual steps as the fallback. Verify: an upgrade with the command on the rehearsal cluster records the platform Deployment, and the `cloud-deploy` workflow then runs. It catches a command whose record or dispatch the live GitHub settings refuse.

## 5. Later phase: deploy tier and prepare Job (D7, D9)

- [ ] 5.1 After `make-cloud-cells-operator-resistant` tasks 4.1 and 4.6, decide the custody of the CI path: an ephemeral private-network peer and a revocable ServiceAccount token for the deploy identity. Then add the path, so that a release that changes `cellctl`'s build inputs and not the chart's rolls `cellctl` as an image-only change. Verify:
  - such a release rolls `cellctl` with no presence step, and records the platform Deployment, which catches a controller-only release that still waits for a person;
  - an unattested image and a change to anything but an image are denied, which catches a CI path that can change more than images.
- [ ] 5.2 Add the pull-only prepare Job: one Job per node per target, before the node's first window. Verify: the first attempt on a node after the Job shows no image pull in its events, and the Job fits the namespace quota. It catches a prepare Job that runs on the wrong node.
- [ ] 5.3 Open a separate OpenSpec change for the catalogue warm-build. Verify: that change decides the cross-process adoption, the second pod's quota and the key handover for tenant-locked cells before any code. It catches a warm-build that ships without a key handover for locked cells.
