## Context

The release path today, from `origin/main`:

- **Rollout.** `cellctl` runs the D6 rollout of `adopt-exomem-cloud-plain-cells`. A cell's target is its `desired_image`, or else `cell_image` (`infra/cellctl/src/cellctl/rollout.py`). The canary is the owner's cell, the row with the lowest `rollout_priority`. One attempt runs fleet-wide at a time.
- **Attempt.** An attempt stops the cell, runs its own backup, applies the target and waits for Ready, up to `cellctl.upgradeReadyDeadlineMinutes`. A failed attempt restores the attempt's backup, starts the previous image and pauses the rollout. A person clears the pause with a new `cell_image` and `clearRolloutPause`.
- **Release route.** Only Substrate's `PUT /api/exomem/admin/cloud-release` writes `cell_image`. It accepts `EXOMEM_DEPLOY_TOKEN` for every action of the route, including `clearRolloutPause`. Nothing calls the route when a release succeeds.
- **Platform.** A person upgrades the platform chart by hand (`docs/runbooks/cloud-platform-deploy.md`). No workflow reaches the cluster. No record states which release the platform runs. The one-way 0.147.0 backup key conversion exists only as runbook prose.
- **Gateway.** Substrate's cloud handler streams each request to the cell. A connect failure returns `503 CELL_NOT_READY` at once. The call to the cell has no timeout, so a hung cell holds a gateway slot until the client leaves.
- **Stop.** A cell is down for 46 seconds to about 8 minutes per upgrade. The stopped backup takes 30–90 s (est.). The first pull of an image on a node takes 30–90 s (est.). A release that changes the retrieval catalogue's identity rebuilds it on first start, in about 8 minutes on one production cell.
- **Traffic.** Only the gateway sees every request, and it stores nothing per cell. The cell counts its traffic in memory and loses the count at each restart.

This change uses these names:
- the **`cloud-deploy` workflow**, which sets `cell_image` after a release (D1);
- the **deploy principal**, Substrate's identity for `EXOMEM_DEPLOY_TOKEN`;
- the **platform Deployment** and the **cell Deployment**, the GitHub Deployment records in the `cloud-platform` and `cloud-deploy` environments (D1, D2);
- the **barrier file**, `infra/contracts/upgrade-barriers-v1.json` (D3);
- a cell's **upgrade window**, one UTC hour in which the cell may upgrade (D4);
- the **due set**, the cells that may start an attempt on this pass (D4);
- the **gateway hold**, the gateway's wait for a cell that refuses connections (D5), and a **held request**, a request inside it;
- the **restore point**, the state that a failed attempt restores (D6);
- the **platform upgrade command**, `infra/scripts/platform_upgrade.py` (D7).

A figure marked "est." is an estimate that no measurement confirms yet.

The engineering primary-source roster was not consulted for this design, because the knowledge base that holds it was unreachable from the authoring session.

## Goals / Non-Goals

**Goals**
- A release reaches every running cell without an operator step, unless it needs a platform upgrade or crosses a barrier.
- Each cell upgrades at most once a day, in its own quiet hour.
- A request that arrives during a cell's switch waits and then succeeds, within a stated budget.
- No automation clears a rollout pause, and no automation rolls back across a barrier.
- Every threshold is a chart value or configuration.

**Non-Goals**
- A canary soak or an error-rate gate. The Ready gate stays. A later change adds stricter criteria as chart values once real users exist.
- Blue/green cells. Cloud cells have no writer lease, so two pods on one volume risk a split write.
- An automated full chart upgrade. It keeps the presence gate of `make-cloud-cells-operator-resistant` D6.
- Cell sleep. D5 only prepares the gateway hold for it.
- Clearing a pause automatically, in any phase.

## Decisions

### D1. A release sets the target image through Substrate's release route

A new workflow, `.github/workflows/cloud-deploy.yml`, runs on three events:
- the release workflow completes with success;
- a platform Deployment reaches the state `success` (D2);
- a person dispatches it, with no inputs.

Each run acts only on the newest published release. Steps:

1. Read the cell image digest that the release run published.
2. Verify the digest's attestation with `gh attestation verify`. The signer must be this repository's release workflow, on the release tag. This is the identity that the deploy tier checks (`make-cloud-cells-operator-resistant` D6).
3. Compare the release with the newest successful cell Deployment. If the release is not newer, set nothing and succeed. A rerun of an old run therefore never moves the fleet back. With no cell Deployment, set nothing, and fail with the missing record named.
4. Read `GET cloud-release`. If the rollout is paused, set nothing, and fail with the pause's error code and held cell (D8).
5. Apply the platform gate (D2) and the barrier check (D3).
6. Send `PUT cloud-release` with `{cellImage}` only.
7. Poll `GET cloud-release` until `last_good_image` equals the target, which means that the canary is Ready on it. Fail on a pause or on the poll timeout.

**Custody.**
- `EXOMEM_DEPLOY_TOKEN` is a secret of the `cloud-deploy` environment. Its deployment branch policy admits only `main`.
- GitHub runs `workflow_run` and `deployment_status` workflows from the default branch, so a branch cannot change the steps.
- The workflow holds no kubeconfig and no private-network peer. It calls Substrate's public admin URL over HTTPS only.
- One concurrency group, without cancel-in-progress, keeps runs in order.
- The job's environment records the cell Deployment of the release. Its status follows the job.
- The poll interval and the poll timeout are repository variables.

**Deploy principal (Substrate).** Today the deploy principal can do everything the release route does. This change narrows it to `GET` and to `PUT` with `cellImage` alone. `clearRolloutPause`, `cellId` and `cellDesiredImage` need the admin token. The operator's rule, "the automation never clears a pause", then holds where the token lands, not only in the workflow's code.

**Why.** Substrate already owns `cell_image` and validates its repository and digest form. The workflow adds only the release event and the attestation check.

**Rejected.**
- `cellctl` polls for new releases. `cellctl` would need egress to GitHub and the registry, and its egress allows only the cluster API, the control database and object storage.
- A workflow with cluster access. It breaks the rule that no workflow reaches the cluster.
- Substrate polls GitHub. Substrate would gain a GitHub dependency and an attestation verifier.
- The workflow's own restraint as the only guard on the pause. A workflow bug or a changed workflow on `main` could send `clearRolloutPause`.

### D2. Cells move only when the platform carries the release

The platform Deployment records which release the platform runs.
- Its ref is the release tag. Its payload lists `barriers_crossed`, the barrier IDs that the upgrade crossed (D3).
- The platform upgrade command creates it after a verified upgrade (D7). Until D7 ships, the runbook ends each manual upgrade with one `gh api` call that creates it.

**The gate.** The workflow compares the release tag with the platform Deployment's tag, over `infra/helm/platform/**` and `infra/cellctl/**`.
- With no difference, cells may move.
- With a difference, or with no platform Deployment, the workflow sets nothing. It raises `cloud-release-platform-needed` and fails.
- When a new platform Deployment succeeds, its `deployment_status` event runs the workflow again. The workflow then sets the image and resolves the alert.

**Alert path.** The workflow posts the alert transition to Substrate's existing alert receiver. It uses its own receiver capability, which the receiver accepts only for the job `cloud-deploy` (Substrate task). The CI log prints no secret and no cell ID.

**Why.** A cell image can depend on controller or chart behaviour of the same release. A source comparison of two tags is exact, and it needs no cluster access.

**Rejected.**
- Comparing live renders. `helm get manifest` reads Secrets, which is admin-tier.
- Version ranges in code. Each would be a special case per release.
- A platform upgrade for every release. It would block every release on a person.

**Limit.** The platform Deployment is a mistake guard. Anyone with repository write can create one. A false record lets cells move onto an older platform. The canary then fails readiness or runs, and D6's restore and pause limit the harm to the owner's cell.

**Cost.** Of the last 40 releases, 10 changed the chart and 15 changed `cellctl`. So about half of releases (est.) wait for a person until the deploy tier handles controller-only releases (D7, later phase).

### D3. Upgrade barriers

**The barrier file**, `infra/contracts/upgrade-barriers-v1.json`, lists each release that is one-way or needs an operator step. Each entry holds:
- `id`;
- `version`: the first release on the far side of the barrier;
- `scope`: `platform` or `cell`;
- `kind`: `one-way` or `operator-step`;
- `runbook`: a runbook path and section that states the step;
- `summary`: one line.

A JSON schema checks the file in CI, and each `runbook` link must resolve. The first entry is the 0.147.0 backup key conversion: `platform`, `one-way`, with the runbook section "Cross the backup key conversion". A task reads the release history for earlier secret-registry changes and adds each as `operator-step`.

**Crossing.** A move from release A to release B crosses an entry when A is older than its `version`, and B is that version or newer.
- The workflow checks `cell` entries between the cell Deployment's release and the new release.
- The platform upgrade command checks `platform` entries between the live platform release and its target (D7).

**Stop.** Crossing an entry stops the automation.
- For a `cell` entry, the workflow sets nothing. It raises `cloud-release-barrier`, names the entry and its runbook, and fails.
- A person does the runbook step, then runs the platform upgrade command with `--crossed <id>`. The platform Deployment then lists the entry, and the workflow treats it as crossed.
- For a `platform` entry, the platform upgrade command refuses to run without `--crossed <id>`.

**No automatic rollback.**
- A crossed `one-way` entry removes `--atomic` from the platform upgrade (D7). Helm then cannot roll back the controller across the change.
- A `cell` entry that is `one-way` changes state outside the cell's volume, or D6's restore would undo it. The platform upgrade command therefore adds its target digest to the chart value `cellctl.forwardOnlyImages`.
- When an attempt toward such a digest misses Ready, `cellctl` pauses the rollout and keeps the cell stopped under its upgrade hold. It records `UPGRADE_FORWARD_ONLY`. It neither restores the volume nor starts the previous image. A person fixes forward.

**Authors.** Every pull request that adds a one-way or operator-step change adds its entry in the same pull request.
- `CONTRIBUTING.md`, which every author reads before a commit, states the rule and names the file. Its review checklist asks for the entry.
- No automated detector exists. Deciding whether a diff is one-way needs judgment. A path list would miss real cases and fire on routine ones.
- D7's secret-contract diff catches one class by machine: a secret-registry change with no entry.

**Rejected.**
- Barriers as runbook prose, as today. No machine can read prose.
- Refusing every chart change. It would block every release.

### D4. Each cell upgrades in its own quiet hour

**The cell counts its traffic.**
- The cell counts each request to its MCP path, per UTC hour. Probes reach other paths, so they never count.
- Each hourly bucket holds the request count and the seconds that the cell ran in that hour. A bucket with no seconds is "not observed", never zero traffic.
- The cell keeps 28 days of buckets in one small file on its own volume. 28 days is the storage bound of the ring, so the chart schema caps the lookback at 28 days.
- The cell replaces the file atomically, at most once a minute. A crash loses at most one minute of counts.
- An unreadable file starts a new ring. The counts are advisory, so their failure never stops the cell.
- A restore of the volume rewinds the history to the restored state. History starts at the release that ships this.

**The readiness `traffic` block** gains:
- `hourly`: the retained buckets, each with its hour, request count and observed seconds;
- `inflight`: the MCP requests that the cell is handling now;
- `last_request_age_seconds`: the age of the newest MCP request, or null when none arrived since the start.

672 buckets take about 20 KB, below the 64 KiB cap of `cellctl`'s readiness read.

The counts are content-free. The gateway already sees the time of each request, so the platform learns nothing new.

**The window.** A pure module beside `rollout.py`, `upgrade_window.py`, computes each cell's window. It reuses `within_backup_window` for the hour test.
1. Take the buckets of the last `lookbackDays`.
2. Coverage is the observed seconds divided by the lookback's seconds. Below `minCoverage`, the window is `defaultHour`, with the source `default`.
3. Otherwise, compute each hour of the day's rate: requests divided by observed seconds, over the buckets of that hour. Skip an hour with no observed seconds.
4. The window is the hour with the lowest rate, with the source `history`.
5. Break a tie by a stable hash of the cell ID, so that cells with equal quiet hours spread across them.

**The cache.**
- `cellctl` writes the window to the StatefulSet annotation `exomem.io/upgrade-window`, as `<hour> history` or `<hour> default`, with the UTC day in `exomem.io/upgrade-window-at`.
- The annotation sits on the StatefulSet's metadata, not on its pod template. A recompute therefore restarts no pod and leaves the render digest unchanged.
- `cellctl` recomputes once per UTC day, or when the annotation is absent. When it cannot read the cell's readiness, it keeps the cached window. With no cached window, it uses `defaultHour`, with the source `default`.
- It logs each recompute with the hour and its source.
- Why a cache: a window that moved during the day could skip or repeat a cell. The annotation survives a `cellctl` restart. The operator sees each cell's window and source with `kubectl`.

**The due set.** A cell is due when its window is open and one of these holds:
- its readiness reports nothing in flight, and no request for `idleMinutes`;
- fewer than `forceMinutes` remain in its window.

Further rules:
- `cellctl` reads the readiness block only of candidates whose window is open, so each pass reads a bounded number of cells.
- An unreadable readiness block counts as not idle.
- A due cell that finds no free slot before its window ends waits for its next window. The stalled alert reports a cell that stays behind (D8).
- `select_upgrade_candidate` takes the due set and starts no attempt for a cell outside it.
- A cell moves straight to the newest target and skips the releases between, as today.

**Exemptions and extensions.**
- **The canary is exempt.** It starts its attempt as soon as its target changes. Under D6 every other cell waits for the canary, so a windowed canary would hold the fleet for up to a day.
- **Render-digest re-applies obey the due set.** A digest change restarts the cell like an upgrade. A key rotation's restarts therefore finish within a day.

**Per-node concurrency.**
- While the canary's image differs from its target, at most one attempt runs fleet-wide: the canary's.
- After that, at most `cellctl.upgradeConcurrencyPerNode` cells per node carry an upgrade or restore hold. A cell's node is the node that its row last observed.
- Hourly backup holds keep their own per-node slots. A cell carries one hold at a time.
- Why: cells cluster by time zone, so many cells share a quiet hour. One attempt fleet-wide gives about 20 to 30 attempts per hour (est.).
- Rejected: a fleet-wide count. One node could take every slot, and its cold starts would contend for CPU.

**Values.** These are chart values with a schema. `cellctl` refuses to start without them, as it does for `upgradeReadyDeadlineMinutes`.

| Value | Default | Range | Meaning |
|---|---|---|---|
| `cellctl.upgradeWindow.enabled` | `true` | — | `false` makes every cell due at once, as before this change |
| `cellctl.upgradeWindow.lookbackDays` | 14 | 1–28 | Days of history behind a window |
| `cellctl.upgradeWindow.minCoverage` | 0.5 | 0–1 | Fraction of the lookback that the cell observed |
| `cellctl.upgradeWindow.defaultHour` | 3 | 0–23 | UTC hour for a cell with too little history |
| `cellctl.upgradeWindow.idleMinutes` | 5 | 1–30 | Idle time before a cell rolls inside its window |
| `cellctl.upgradeWindow.forceMinutes` | 10 | 1–30 | Minutes before the window ends when a cell rolls even if busy |
| `cellctl.upgradeConcurrencyPerNode` | 1 | 1–8 | Upgrade or restore holds per node after the canary |

**Rejected.**
- A fixed UTC hour for the fleet. Users are international.
- A Substrate table of hourly traffic per cell. The control plane does not need the history, and the cell's volume needs no migration.
- A window from the gateway's counts. The gateway stores nothing per cell today.

### D5. The gateway holds a request while its cell restarts

This is the core of a seamless upgrade. It lives in Substrate's cloud handler, between the gateway and the cell. Traefik sits in front of the gateway, so it cannot see the cell's refusal.

**Which requests.** A request is holdable when its body is at most `EXOMEM_CLOUD_HOLD_BODY_MAX_BYTES`.
- The gateway reads up to the cap plus one byte, and buffers a body within the cap.
- A larger body streams as today, and that request is never held.
- Response bodies always stream.

**When to retry.** The gateway retries only on a connect error, when no byte reached the cell: a refused connection, a reset before the connection opened, a DNS failure for the cell's Service, or the connect timeout. It never retries after the connection opened, because the cell may have acted.

**How.**
- The gateway retries every `EXOMEM_CLOUD_HOLD_INTERVAL_MS`, with random jitter of up to `EXOMEM_CLOUD_HOLD_JITTER_MS`.
- It retries until `EXOMEM_CLOUD_HOLD_BUDGET_SECONDS` runs out. The proposed start is 40 s, inside the 30–45 s range. A budget of 0 turns the hold off.
- When the budget runs out, it answers `503 CELL_NOT_READY` with `Retry-After: EXOMEM_CLOUD_HOLD_RETRY_AFTER_SECONDS`.
- When the client disconnects, it stops at once.
- It retries only toward the same cell, so the hold keeps the rule that a routing failure never crosses tenants.

**Own caps.**
- A held request counts against `EXOMEM_CLOUD_HOLD_MAX` and `EXOMEM_CLOUD_HOLD_MAX_PER_TENANT`, not against the in-flight caps, until the cell accepts its connection.
- A request over a hold cap gets the `503` with `Retry-After` at once.
- Why: a restarting cell must not occupy the in-flight slots that other tenants need, and one tenant's burst must not fill the hold.

**Timeouts.**
- `EXOMEM_CLOUD_CONNECT_TIMEOUT_MS` bounds each connect attempt.
- `EXOMEM_CLOUD_UPSTREAM_TIMEOUT_SECONDS` bounds the time from the first forwarded byte to the response headers. When it runs out, the gateway answers `504 CELL_TIMEOUT` and frees its slot.

**Configuration.** The platform chart renders each value into the gateway's environment, from `cloudGateway.hold.*` and `cloudGateway.upstream.*`. C3's pinned environment set grows to match.

**One mechanism.** A future cell sleep would wake a cell and answer `CELL_WAKING` while it starts. That case adds a trigger to this hold, not a second wait loop.

**Rejected.**
- Traefik's retry middleware. It sees only the gateway.
- An Envoy sidecar with a connect-failure retry policy. It adds a proxy hop and a component for one retry loop.
- Retrying only in clients. Many MCP clients surface the error to the agent and do not retry.
- Retrying any failure. A tool call that reached the cell could run twice.

**Old debt.** The in-flight caps, 16 in total and 4 per tenant, are constants in the handler's code. This change does not move them, and reports them as debt.

### D6. The restore point is a snapshot taken after the stop

**Local cells** use a storage class with snapshot support. Their attempt becomes:

1. Set the upgrade hold, scale to 0, and wait until no pod uses the volume, as today.
2. Take a VolumeSnapshot of the volume. Record its name in `exomem.io/pre-upgrade-volume-snapshot`, and wait until it is ready. A snapshot was ready in 1.4–2.2 s in the local-storage spike.
3. Apply the target at once, as today's step 3.
4. Create a read-only clone claim from the snapshot, and run the restic backup Job on it, as the hourly hold does. Record the restic snapshot ID in `exomem.io/pre-upgrade-snapshot`, and write `last_backup_at` and `last_backup_snapshot`. A failure here does not end the attempt. The backup-age alert covers a cell whose backups keep failing.
5. When the cell is Ready, delete the clone and the snapshot once the backup Job has ended.
6. When Ready does not arrive, restore from the snapshot:
   - A restore Job mounts a clone of the snapshot read-only, and copies `/data/vault` and `/data/host` onto the cell's volume. It deletes the files that the target created.
   - It runs on the cell's node, as UID 10001, and leaves the volume root untouched.
   - If the snapshot is gone, it restores the restic snapshot from step 4. If neither exists, the attempt records `RESTORE_FAILED`, as today.

**Rules.**
- The snapshot is never taken while the cell serves. A live snapshot is crash-consistent only (local-storage D9). The stop makes it consistent.
- Both restore points belong to the attempt. `last_backup_snapshot` never replaces them, as in plain-cells D6.
- The clone uses the second claim that the cell's quota already admits (local-storage D3).
- The change saves the backup Job's 30–90 s (est.) from each stop.

**Cells on Hetzner volumes** cannot take a CSI snapshot. They keep the stopped restic backup as the restore point. That backup no longer runs `forget --prune`. Local-storage D3's retention rule already says so, but `_backup_source` in `reconcile.py` still prunes. Their nightly backup keeps its prune.

**Rejected.**
- A snapshot while the cell serves. It is crash-consistent only.
- The full stopped restic backup on local cells. It adds 30–90 s (est.) to each stop.
- A restore that swaps the cell's claim. The StatefulSet's claim is fixed, and a claim change is the heavier move procedure.

### D7. A person upgrades the platform with one command

`infra/scripts/platform_upgrade.py <release tag>` runs in an admin session with the operator's presence (`make-cloud-cells-operator-resistant` D6).

**Machine review.** Each step stops the command on a failure, before any change:
1. Check out the tag, and build the chart dependencies.
2. Render the chart, and check it against its schema.
3. Run `helm diff upgrade` against the live release. Write the diff to the private upgrade directory, and print the changed objects.
4. Run a server-side dry run.
5. Probe Substrate's columns: run the release's `cellctl` column check, read-only, against the control database.
6. Compare the secret contracts of the live release and the tag. A change with no barrier entry stops the command and names the omission.
7. List the `platform` barriers between the live release and the tag (D3). Each one needs `--crossed <id>`.

The command reads the live release from Helm's release metadata, not from the platform Deployment. A wrong record therefore cannot mislead it.

**Upgrade.**
- Apply the namespaces. Then run `helm upgrade` with `--wait` and the timeout from a value.
- Add `--atomic` unless a crossed barrier is `one-way`.
- Use every post-renderer that the chart's local-storage path requires.
- For a crossed `cell` barrier that is `one-way`, add the target digest to `cellctl.forwardOnlyImages` (D3).

**Verify and record.**
- Wait until `cellctl` and the gateway are Ready.
- Then create the platform Deployment for the tag, with `barriers_crossed`, in the state `success`. A failed upgrade records the state `failure`.

**`cellctl` refuses a missing column.** At start, `cellctl` checks every column it reads or writes, and refuses to start when one is missing. A schema mismatch then fails the new pod, and `--atomic` keeps the old controller.

**Why.** The runbook's upgrade has many manual steps with judgment points. One command makes the review repeatable and records the outcome.

**Rejected.**
- Flux or Argo CD running the chart (see Maintained tools).
- A CI job with cluster administration. It contradicts the presence gate.

**Later phase: the deploy tier.** The chart's `operatorDeploy` tier changes images only, and a verifier accepts only attested images. A CI path to it would use an ephemeral private-network peer and a revocable ServiceAccount token.
- With it, a release that changes `infra/cellctl/**` and not `infra/helm/platform/**` rolls `cellctl` with no presence step, and records the platform Deployment.
- It depends on `make-cloud-cells-operator-resistant` task 4.1 (the verifier and the image-only admission rule) and task 4.6 (release-ref protection).
- It also needs the custody decision for the CI peer and token.

### D8. Rollout alerts

`cellctl` raises four alerts through the existing alert receiver. It sends the same content-free transition as its backup-age alert, and lists the affected cells by ID in its log.

| Alert | Fires while |
|---|---|
| `cloud-rollout-paused` | The rollout row is paused, for any error code |
| `cloud-rollout-held` | A cell's upgrade or restore hold is older than `alerts.rollout_held_warn_seconds` |
| `cloud-rollout-stalled` | The rollout is not paused, and a candidate has stayed behind its target for longer than `alerts.rollout_stalled_warn_seconds` |
| `cloud-rollout-canary-waiting` | The rollout is not paused, and the canary has stayed behind its target for longer than `alerts.canary_wait_warn_seconds` |

**Rules.**
- "Stayed behind" reads the StatefulSet annotation `exomem.io/candidate-since`. `cellctl` sets it when a cell first becomes a candidate, and removes it when the cell runs its target. A newer target keeps the earlier time, because the cell is still behind.
- The thresholds live in the observability contract, `infra/contracts/observability-v1.json`. The chart renders them into `cellctl`'s environment, and `cellctl` refuses to start without them.
- A pass that could not observe every cell sends no transition, so it resolves no alert. This is the rule of the existing alerts.
- A person clears the pause. Neither `cellctl` nor the workflow clears it.

**The workflow** also reports a pause. It fails when it finds the rollout paused (D1 step 4), or when the rollout pauses during its poll (D1 step 7).

### D9. Later: prepare the cell before its stop

These steps are not in this change's first phases.

- **Pull-only prepare Job.** Before a node's first window for a target, a Job with the target image runs a no-op on that node. The node then holds the image when the stop comes.
  - Saving: the first pull of each image on a node, 30–90 s (est.).
  - Cost: one Job per node per release, and old images that the kubelet's image collection must remove.
- **Catalogue warm-build.** A prepare Job builds the target's catalogue on the cell's volume, and the new pod adopts it.
  - Saving: about 8 minutes on a release that changes the catalogue's identity. 10 of 156 releases since 2026-09-10 did.
  - Cost: cross-process adoption code, a second pod in the cell's quota (above 1.2 GiB), and a key handover for tenant-locked cells.

## What "seamless" means

- A typical local cell stops for about 25–60 s (est.), and only after its idle minutes, or at its window's end.
- A request in that gap waits in the gateway hold, then succeeds.
- A request that waits past the hold budget gets `503` with `Retry-After`. So does a request whose body is over the cap.
- A release that changes the catalogue's identity still stops a cell for about 8 minutes, until D9's warm-build.
- It is unverified whether a 30–45 s hold fits client tool-call timeouts. Task 1.4 measures it.

## Maintained tools considered

| Tool | What it would give | Gap |
|---|---|---|
| Argo CD | Git-driven sync, with sync windows | The chart's `lookup` guards render empty in Argo CD. Sync windows are fixed schedules, not learned per cell. Its controller needs cluster administration, which breaks the presence gate |
| Flux helm-controller | Unattended chart upgrades | It contradicts the presence gate. Its automatic rollback is wrong across a one-way barrier. It has no exec post-renderer for the local-storage path |
| Keel | Image updates on a new tag | It verifies no attestation, and it bypasses the attempt's backup and restore |
| Flagger, Argo Rollouts | Canary analysis and traffic shifting | Both shift traffic between stateless replicas. A cell has one writer on one volume |
| Helm rollback | Undo of a chart upgrade | It restores an older controller but not the control database, which breaks across a one-way change |

**Reused.** `helm-diff`, GitHub Deployments, `gh attestation verify`, the existing alert receiver, `within_backup_window`, and the hourly hold's clone and backup Job. The later deploy tier reuses ephemeral private-network peer keys.

**Custom work**, and its maintenance cost:
- The window module and the due set. No tool learns a quiet hour per tenant for a single-writer cell. Cost: one pure module with unit tests.
- The gateway hold. Cost: one retry loop with caps in the handler.
- The platform upgrade command. Cost: one script that wraps the runbook's steps.

## Scale and load target

- **Load target.** About 43 cells per node, at 10 GiB each (local-storage D6). Cells cluster by time zone. Releases come about 3 times a day.
- **Data size.** 672 buckets per cell, about 20 KB per readiness read.
- **`cellctl` passes.**
  - The daily recompute reads each cell's readiness once a day.
  - Each pass reads readiness only for candidates in an open window.
  - No pass lists history across cells.
- **Throughput.**
  - An attempt takes about 2–3 minutes (est.), so one slot per node rolls about 20–30 cells an hour (est.).
  - If all 43 cells on a node share one hour, about half of them roll, and the rest wait a day (est.). The tie-break spreads tied hours, and `upgradeConcurrencyPerNode` can rise.
  - A catalogue identity release rolls about 7 cells per node-hour (est.).
- **Gateway latency.**
  - A normal request gains one buffered read of at most the body cap, and no extra round trip.
  - Only a request that meets a restart waits, and for at most the hold budget.
  - The hold caps protect the in-flight slots of other tenants.
- **Workflow.** It polls one route every poll interval, up to its timeout.
- **Old debt.**
  - `GET cloud-release` returns every cell, so its response grows with the fleet. A bounded rollout-only view is a Substrate follow-up.
  - The gateway's in-flight caps are constants in code (D5).

## Controls added

| Control | Prevents | Wrong-firing cost, and who pays |
|---|---|---|
| Attestation check (D1) | An unattested image in the fleet | A release does not deploy. The operator reruns the workflow |
| Newer-release check (D1) | A rerun that moves the fleet back | None. An equal release sets nothing |
| Deploy principal scope (D1) | Automation that clears a pause | None for automation. A person uses the admin token |
| Platform gate (D2) | A cell image on a platform that lacks its controller or chart | Tenants get the release later. The operator runs a platform upgrade |
| Barrier stop (D3) | A one-way or operator-step crossing without its step | The same as the platform gate |
| Forward-only images (D3) | Restoring a volume onto external state that the previous image cannot read | The owner's cell stays down until a person fixes forward |
| Hold caps (D5) | A restart that exhausts other tenants' slots | A held request gets `503` with `Retry-After` sooner |
| Missing-column refusal (D7) | A controller that acts on half a schema | `--atomic` keeps the old controller. The operator migrates first |

## Risks / Trade-offs

- **The hold budget may outlast client timeouts.** Then a client gives up before the cell returns. Task 1.4 measures the clients, and the budget is configuration.
- **A request inside the cell during its stop** fails without a retry. The idle rule makes this rare. The cell drains its in-flight requests at termination (unverified).
- **About half of releases wait for a person** until the deploy tier ships (D2).
- **The Deployment records are mistake guards** (D2). A false record moves cells early, and D6's canary restore limits the harm.
- **Windows cluster.** A node whose cells share one quiet hour needs several days for a catalogue identity release (est.). The stalled alert reports a cell that stays behind.
- **A default hour may be busy** for a new cell's tenant. It lasts until the cell observes enough of its lookback.
- **Daylight saving** moves a local quiet hour by one UTC hour. The daily recompute follows it after the lookback.
- **A forward-only failure** keeps the owner's cell down until a person acts (D3).
- **A key rotation** now takes up to a day to restart every cell (D4).

## Migration Plan

1. Ship Substrate: the deploy principal's narrower scope, and the gateway hold with its budget at 0.
2. Release the cell's traffic counts, so that history starts.
3. Release `cellctl`'s window, due set, concurrency and alerts. Run the platform upgrade by hand.
4. Record the first platform Deployment and the first cell Deployment with the runbook's `gh api` calls. Each names the release that is live.
5. Set the hold budget to its first value, and run the measurement of task 1.4.
6. Create the `cloud-deploy` environment with its branch policy, and add `EXOMEM_DEPLOY_TOKEN` and the receiver capability.
7. Rehearse a release end to end on a disposable cell (task 2.1).
8. Ship the restore point (phase 2), then the platform upgrade command (phase 3).

**Rollback.**
- Remove the token from the `cloud-deploy` environment, and the automation stops.
- Set `cellctl.upgradeWindow.enabled: false`, and cells roll as soon as they are eligible.
- Set the hold budget to 0, and the gateway answers at once, as before.

## Archive order

`cloud-cell` lives only in the delta of `adopt-exomem-cloud-plain-cells`. `move-cloud-cells-to-local-storage` modifies its backup requirement, and this change modifies the same requirement for the pre-upgrade restore point (D6). This change therefore archives after both changes. Phase 2 needs local cells in any case. If a later change edits the backup requirement first, refresh this delta's copy and keep that change's additions. If an archive tool refuses the order, this change's `cloud-cell` delta folds into the earlier change before it archives.

## Open Questions

- **First values.** The default hour, the minimum coverage and the hold budget are proposed values. Task 1.4 and the first weeks of history may move them. Each is a chart value or configuration, so a change needs no code.
- **Barrier history.** Which earlier releases changed the secret registry. Task 1.8 reads the history.
- **Deploy tier custody.** The CI peer and the token's revocation path (D7, later phase).
