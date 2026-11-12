## Purpose

Moves each successful Exomem release into Cloud's `cell_image` without an operator step, unless the release needs a platform upgrade or crosses an upgrade barrier, and alerts the operator when a rollout stops.

## ADDED Requirements

### Requirement: A release sets the cell image only after its attestation verifies

The `cloud-deploy` workflow SHALL run on a dispatch on `main`. The release workflow's tag run SHALL send that dispatch after it publishes and attests the release's images. It SHALL act only on the newest published release. It SHALL set `cell_image` through Substrate's release route only after the cell image's attestation verifies as this repository's release workflow on the release tag. It SHALL send the digest with its verified release tag.

#### Scenario: An attested release passes every gate

- **WHEN** a release succeeds, its cell image's attestation verifies, and neither the platform gate nor a barrier stops it
- **THEN** the workflow sets `cell_image` to the release's cell image digest, with the release tag

#### Scenario: The attestation does not verify

- **WHEN** the release's cell image digest has no attestation from the release workflow on the release tag
- **THEN** the workflow sets nothing and fails

#### Scenario: A push to main publishes no release

- **WHEN** the release workflow completes on `main` without a release tag
- **THEN** the `cloud-deploy` workflow does not run

#### Scenario: The newest release is already the cell image

- **WHEN** the rollout view's `cell_image` equals the newest release's digest
- **THEN** the workflow sets nothing and succeeds

### Requirement: Substrate never sets a digest twice for the deploy principal

Substrate SHALL record each digest the first time it becomes `cell_image`, with its release tag and the time. Before the deploy principal can use the history, a seed SHALL add every published release's cell image digest and tag, the live `cell_image` and `last_good_image`, and each cell's observed and desired images. A seeded digest that matches no published release SHALL carry no tag. The history therefore holds every digest that was ever `cell_image`. Substrate SHALL refuse a deploy-token request whose digest the history holds, with `CELL_IMAGE_SEEN`, and SHALL write nothing. Substrate SHALL also refuse a deploy-token request whose release tag is older than the tag of the current `cell_image`, with `CELL_RELEASE_OLDER`, and SHALL write nothing. This covers a release that published but never became `cell_image`. The admin token MAY set any digest. A request that sets a digest new to the history SHALL carry its release tag. cellctl's database role SHALL have read access to the history.

#### Scenario: A release from before the history

- **WHEN** the deploy token sends the digest of a release that published before the history shipped
- **THEN** Substrate refuses it with `CELL_IMAGE_SEEN`, and `cell_image` keeps its value

#### Scenario: A skipped older release is sent

- **WHEN** the deploy token sends the digest of a release that never became `cell_image` and whose tag is older than the current `cell_image` tag
- **THEN** Substrate refuses it with `CELL_RELEASE_OLDER`, and `cell_image` keeps its value

#### Scenario: An old run is rerun

- **WHEN** the workflow sends a digest that was `cell_image` before
- **THEN** Substrate refuses it with `CELL_IMAGE_SEEN`, `cell_image` keeps its value, and the workflow succeeds without a change

#### Scenario: A person withdrew a release

- **WHEN** a person set `cell_image` back to an earlier digest with the admin token, and a later merge runs the workflow for the withdrawn release
- **THEN** Substrate refuses the withdrawn digest, and the fleet stays on the earlier digest

#### Scenario: A newer release follows a withdrawal

- **WHEN** a release newer than the withdrawn one publishes and passes every gate
- **THEN** Substrate accepts its digest, and the fleet moves forward

### Requirement: The release automation holds no cluster access and only main can use its token

The `cloud-deploy` workflow SHALL run in the `cloud-deploy` GitHub environment, whose deployment branch policy admits only `main`. Only that environment SHALL hold the deploy token. The workflow SHALL reach Substrate over HTTPS only, and SHALL hold no cluster credential and no private-network peer.

#### Scenario: A branch workflow asks for the token

- **WHEN** a workflow that runs on a ref other than `main` asks for the `cloud-deploy` environment
- **THEN** GitHub refuses the environment, and the job receives no deploy token

#### Scenario: The workflow tries to reach the cluster

- **WHEN** the workflow's job runs
- **THEN** it holds no kubeconfig and no private-network key, and it calls only Substrate's HTTPS admin route

### Requirement: The release automation reads only a bounded view and prints no tenant identifier

Substrate SHALL serve a bounded rollout view. It SHALL return `cell_image` with its release tag, the pause state and its error code, and whether the canary runs its target and is Ready. It SHALL also return counts of cells on target, behind target and under a hold. The view SHALL return no cell ID and no tenant ID. The canary signal SHALL come from the canary row's own observed image and readiness. The deploy principal SHALL read only this view, not the full release view. The workflow SHALL print no cell ID, no tenant ID and no secret.

#### Scenario: The workflow polls the rollout

- **WHEN** the workflow polls after it sets `cell_image`
- **THEN** it reads the rollout view, and the answer holds no cell ID and no tenant ID

#### Scenario: Another cell becomes Ready before the canary

- **WHEN** a new cell becomes Ready on the new `cell_image` while the canary is still behind
- **THEN** the rollout view reports the canary as not on target, and the workflow keeps polling

#### Scenario: The deploy principal reads the full view

- **WHEN** a request with the deploy token reads the full release view
- **THEN** Substrate refuses it

### Requirement: The release automation never clears a rollout pause

Substrate's deploy principal SHALL be able to read the rollout view and to set `cellImage` with its release tag, and nothing else. Clearing a rollout pause and setting one cell's image SHALL need the admin token. While the rollout is paused, the workflow SHALL set nothing, and SHALL fail with the pause's error code alone.

#### Scenario: The deploy principal tries to clear a pause

- **WHEN** a request with the deploy token sends `clearRolloutPause`
- **THEN** Substrate refuses it, and the rollout stays paused

#### Scenario: The deploy principal tries to set one cell's image

- **WHEN** a request with the deploy token sends `cellId` and `cellDesiredImage`
- **THEN** Substrate refuses it and writes nothing

#### Scenario: The workflow finds the rollout paused

- **WHEN** the workflow reads a paused rollout before it sets the image
- **THEN** it sets nothing, and fails with the pause's error code, and its log names no cell

#### Scenario: The rollout pauses while the workflow polls

- **WHEN** the rollout pauses before the canary runs the target
- **THEN** the workflow fails with the pause's error code, and the pause stays until a person clears it

### Requirement: Cells move only when the platform carries the release

The workflow SHALL set `cell_image` only when the newest successful platform Deployment names a release whose chart and controller build inputs equal the new release's. The build inputs SHALL be the files under `infra/helm/platform/` and `infra/cellctl/`, except Markdown files and test directories. Otherwise, or with no platform Deployment, the workflow SHALL set nothing and raise `cloud-release-platform-needed` through the alert receiver. After a person upgrades the platform, the platform upgrade command or the runbook step SHALL dispatch the workflow on `main`. The workflow SHALL then resolve the alert when the gate passes.

#### Scenario: The release changes no platform build input

- **WHEN** the new release and the platform Deployment's release hold the same chart and controller build inputs
- **THEN** the workflow sets `cell_image`

#### Scenario: The release changes the chart

- **WHEN** the new release changes a template or value file under `infra/helm/platform/` since the platform Deployment's release
- **THEN** the workflow sets nothing, raises `cloud-release-platform-needed`, and fails

#### Scenario: The release changes only controller tests and docs

- **WHEN** the new release changes only files under `infra/cellctl/tests/` and Markdown files since the platform Deployment's release
- **THEN** the platform gate passes

#### Scenario: A person upgrades the platform

- **WHEN** a person records a successful platform Deployment for the new release and dispatches the workflow on `main`
- **THEN** the workflow runs, sets `cell_image`, and resolves `cloud-release-platform-needed`

#### Scenario: No platform Deployment exists

- **WHEN** the workflow runs and no successful platform Deployment exists
- **THEN** it sets nothing and raises `cloud-release-platform-needed`

### Requirement: Upgrade barriers stop the automation

The barrier file SHALL list each release that is one-way or needs an operator step, with its scope, kind and runbook section. A pull request that adds such a change SHALL add its entry. From phase 1, the workflow SHALL read the barrier file. It SHALL set nothing while an entry of either scope lies at or below the new release and no successful platform Deployment lists it in `barriers_crossed`. The platform upgrade command SHALL refuse to cross a `platform` entry without `--crossed`. Crossing a one-way entry SHALL never roll back automatically.

#### Scenario: A release crosses an uncrossed barrier

- **WHEN** an entry lies at or below the new release, and no successful platform Deployment lists it as crossed
- **THEN** the workflow sets nothing, raises `cloud-release-barrier` with the entry's ID, and fails

#### Scenario: A platform barrier outside the gated paths

- **WHEN** an uncrossed `platform` entry lies at or below a release that changes no chart or controller build input
- **THEN** the workflow sets nothing and raises `cloud-release-barrier`

#### Scenario: A person crossed the barrier

- **WHEN** a successful platform Deployment lists the entry in `barriers_crossed`
- **THEN** the workflow treats the entry as crossed and sets `cell_image`

#### Scenario: An entry names a missing runbook section

- **WHEN** a barrier entry's `runbook` names a section that does not exist
- **THEN** the barrier file's CI check fails

#### Scenario: The platform crosses a one-way barrier

- **WHEN** the platform upgrade command crosses a `platform` entry of kind `one-way`
- **THEN** it upgrades without `--atomic`, so Helm cannot roll the controller back across it

### Requirement: A person upgrades the platform with one reviewed command

The platform upgrade command SHALL run in an admin session with the operator's presence. Before any change, it SHALL render and schema-check the chart, diff it against the live release, run a server dry run, probe the control database's columns, diff the secret contracts, and check `platform` barriers. It SHALL upgrade with `--atomic` unless a crossed barrier is one-way. It SHALL record the platform Deployment of the upgrade's outcome, and after a success it SHALL dispatch the `cloud-deploy` workflow on `main`.

#### Scenario: The control database lacks a column

- **WHEN** the release's controller reads a column that the control database does not have
- **THEN** the command stops before any change and names the column

#### Scenario: A secret contract changes without a barrier

- **WHEN** the tag's secret contracts differ from the live release's, and no barrier entry covers the change
- **THEN** the command stops before any change and names the omission

#### Scenario: A clean upgrade

- **WHEN** every review step passes, and no crossed barrier is one-way
- **THEN** the command upgrades with `--atomic`, records a successful platform Deployment for the tag, and dispatches `cloud-deploy` on `main`

#### Scenario: The upgrade fails

- **WHEN** the upgrade does not become Ready within its timeout
- **THEN** the command records a failed platform Deployment, dispatches nothing, and the newest successful platform Deployment keeps its release

#### Scenario: The controller starts against an older schema

- **WHEN** `cellctl` starts and a column that it reads or writes is missing
- **THEN** it refuses to start and names the column

### Requirement: The controller alerts when a rollout stops

`cellctl` SHALL raise `cloud-rollout-paused`, `cloud-rollout-held`, `cloud-rollout-stalled` and `cloud-rollout-canary-waiting` through the existing alert receiver, as content-free transitions. Their thresholds SHALL come from the observability contract, and `cellctl` SHALL refuse to start without them. A pass that did not observe every cell SHALL send no transition. Neither `cellctl` nor the workflow SHALL clear a pause.

#### Scenario: The rollout pauses

- **WHEN** an attempt pauses the rollout, with any error code including `UPGRADE_FORWARD_ONLY`
- **THEN** `cloud-rollout-paused` fires, and it resolves only after a person clears the pause

#### Scenario: A hold lasts too long

- **WHEN** a cell's upgrade or restore hold is older than the configured held threshold
- **THEN** `cloud-rollout-held` fires, and `cellctl`'s private log names the cell

#### Scenario: A cell stays behind its target

- **WHEN** the rollout is not paused, and a candidate has stayed behind its target for longer than the configured stalled threshold
- **THEN** `cloud-rollout-stalled` fires
- **AND** it resolves when no candidate exceeds the threshold

#### Scenario: A newer target arrives for a cell that is behind

- **WHEN** `cell_image` changes while a cell is still behind its earlier target
- **THEN** the cell's time behind keeps counting from when it first fell behind

#### Scenario: The canary does not take the target

- **WHEN** the rollout is not paused, and the canary has stayed behind its target for longer than the configured canary threshold
- **THEN** `cloud-rollout-canary-waiting` fires

#### Scenario: A pass cannot observe a cell

- **WHEN** a pass fails to observe any non-deleted cell
- **THEN** `cellctl` sends no rollout alert transition, and no firing alert resolves
