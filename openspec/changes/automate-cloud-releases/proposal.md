## Why

An Exomem release does not reach Cloud cells today unless the operator acts. The release workflow publishes and attests the cell image, but nothing sets it as Substrate's `cell_image`. The operator upgrades the platform chart by hand, and no record states which release the platform runs. When `cellctl` rolls a cell, the tenant gets an immediate `503 CELL_NOT_READY` for 46 seconds to about 8 minutes, at any hour.

The operator decided on 2026-11-10 that cells upgrade automatically, once a day, each in its own quiet hour. Upgrades are to be seamless, so that users get each release's benefits without noticing the switch. The service is not public yet, so the canary and health criteria stay loose. Every threshold is configuration, so that it can tighten once real users exist.

## What Changes

- **A release sets the cell image.** A new `cloud-deploy` workflow runs when a release succeeds on its tag. It verifies the cell image's attestation, then sets `cell_image` through Substrate's existing release route. It runs in a GitHub environment that only `main` can use, over HTTPS only, with no cluster access.
- **The automation never clears a pause and never moves backwards.** Substrate's deploy principal can set a new `cellImage` with its release tag. It can read a bounded rollout view with no tenant identifiers. Substrate keeps a history of every digest that was `cell_image`, and refuses the deploy principal any digest in it. Clearing a rollout pause, setting a cell's own image and setting an old digest need the admin token, which a person holds.
- **Cells wait for their platform.** Cells move only when the deployed platform carries the release's chart and controller build inputs. Otherwise an alert says that a platform upgrade is needed. A GitHub Deployment records the deployed platform release, and each platform upgrade dispatches the workflow again.
- **Upgrade barriers.** A contract file lists each release that is one-way or needs an operator step. Crossing one stops the automation. A cell whose previous image is older than a one-way barrier never restores to it automatically: it holds until a person acts.
- **Each cell upgrades in its own quiet hour.** The cell counts its requests per UTC hour on its own volume. `cellctl` computes each cell's quietest hour over the last 14 days. Inside that hour, it rolls the cell after a few idle minutes, or at the hour's end. A cell with too little history uses a default hour, and its record says so.
- **Pinned attempts, then more attempts at once.** Each attempt pins its target when it starts, and a pause ends every attempt that has not applied its target. After that ships, `cellctl` allows a configured number of attempts per node once the canary runs the target.
- **The gateway holds requests during the switch.** Substrate's gateway retries a request whose connection the cell refused, until a budget runs out. It then answers `503` with `Retry-After`. The gateway also gains an upstream timeout.
- **A faster restore point.** On local cells, the restore point becomes a volume snapshot, taken after the stop. The off-site backup runs from its clone after the target starts. Cells on Hetzner volumes keep the stopped backup, without its prune.
- **One command upgrades the platform.** A person with presence runs one command. It reviews the release by machine, upgrades, and records the Deployment. The image-only deploy tier, which needs no presence for controller-only releases, is a later phase.
- **Alerts.** `cellctl` raises an alert when the rollout pauses, holds, stalls or waits on its canary. The workflow fails visibly while the rollout is paused.
- **Later.** A prepare Job pulls the target image before the window. A later step builds the catalogue ahead of the stop.
- **Default-off and soft-fail.** The workflow does nothing until its environment holds the deploy token. A hold budget of 0 turns the gateway hold off. The chart value `cellctl.upgradeWindow.enabled: false` rolls cells as soon as they are eligible, as today.

## Capabilities

### New Capabilities

- `cloud-release-automation`: the `cloud-deploy` workflow, the deploy principal's scope, the platform gate and its Deployment record, the barrier file, the platform upgrade command and the rollout alerts.

### Modified Capabilities

- `cloud-cell`: the release requirement gains upgrade windows, per-node concurrency after the canary and the snapshot restore point. The backup requirement takes a local cell's pre-upgrade off-site backup from the snapshot, after the target starts. New requirements cover the cell's hourly traffic counts and each cell's upgrade window. `cloud-cell` is not canonical yet. It lives in the delta of `adopt-exomem-cloud-plain-cells`, and `move-cloud-cells-to-local-storage` modifies the backup requirement. This change archives after both.
- `hosted-gateway-contract`: the gateway holds a request while its cell restarts, and every call to a cell has a timeout.

### Modified elsewhere

This delivery adds pointer text to the design of each change below. It ticks no task. The implementation delivery makes the other edits.

- `adopt-exomem-cloud-plain-cells`:
  - design D6: upgrade windows, per-node concurrency after the canary, the snapshot restore point, and the deploy principal that cannot clear a pause;
  - design C3: the gateway hold buffers a small request body and retries a refused connection.
- `move-cloud-cells-to-local-storage` design D3: a local cell's pre-upgrade backup becomes a snapshot taken after the stop. The pre-upgrade backup of a Hetzner cell stops pruning, as D3's retention rule already states.
- `make-cloud-cells-operator-resistant` design D6: the full chart upgrade keeps its presence gate. The platform upgrade command runs under it. Cell releases use Substrate's deploy principal, which holds no cluster access.

## Impact

- **Substrate (separate repository):** the deploy principal's narrower scope, the image history, the bounded rollout view, the gateway hold and timeouts, and a receiver capability for the `cloud-deploy` job.
- **Exomem cell:** hourly request counts on the cell's volume, a history path on the health port, and two readiness `traffic` fields.
- **cellctl:** pinned attempts and the pause abort, a pure window module, the due set, the forward-only rule, per-node concurrency, the snapshot restore point and four rollout alerts.
- **Chart:** the window, concurrency and gateway hold values, and their schema.
- **Contracts:** `infra/contracts/upgrade-barriers-v1.json`, and four alert thresholds in the observability contract.
- **CI:** the `cloud-deploy` workflow, and the `cloud-deploy` and `cloud-platform` GitHub environments.
- **Scripts and runbooks:** the platform upgrade command, and updates to `docs/runbooks/cloud-platform-deploy.md`.
- **Not changed:** the release workflow, unless task 2.1 shows that its tag run starts no `workflow_run`; then it gains one dispatch step. Also not changed: the D6 canary rule and its restore-based rollback, the Ready deadline, and the presence gate on a full chart upgrade.
