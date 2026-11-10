## Purpose

Moves each successful Exomem release into Cloud's `cell_image` without an operator step, unless the release needs a platform upgrade or crosses an upgrade barrier, and alerts the operator when a rollout stops.

## ADDED Requirements

### Requirement: A release sets the cell image only after its attestation verifies

The `cloud-deploy` workflow SHALL run when a release succeeds, when a platform Deployment succeeds, and on dispatch. It SHALL act only on the newest published release. It SHALL set `cell_image` through Substrate's release route only after the cell image's attestation verifies as this repository's release workflow on the release tag. It SHALL set no release that is not newer than the newest successful cell Deployment.

#### Scenario: An attested release passes every gate

- **WHEN** a release succeeds, its cell image's attestation verifies, and neither the platform gate nor a barrier stops it
- **THEN** the workflow sets `cell_image` to the release's cell image digest
- **AND** it records a successful cell Deployment of the release

#### Scenario: The attestation does not verify

- **WHEN** the release's cell image digest has no attestation from the release workflow on the release tag
- **THEN** the workflow sets nothing and fails

#### Scenario: An old run is rerun

- **WHEN** the workflow runs for a release that is not newer than the newest successful cell Deployment
- **THEN** it sets nothing, and `cell_image` keeps its value

#### Scenario: No cell Deployment exists

- **WHEN** the workflow runs and no successful cell Deployment exists
- **THEN** it sets nothing and fails, naming the missing record

### Requirement: The release automation holds no cluster access and only main can use its token

The `cloud-deploy` workflow SHALL run in the `cloud-deploy` GitHub environment, whose deployment branch policy admits only `main`. Only that environment SHALL hold the deploy token. The workflow SHALL reach Substrate over HTTPS only, and SHALL hold no cluster credential and no private-network peer.

#### Scenario: A branch workflow asks for the token

- **WHEN** a workflow that runs on a ref other than `main` asks for the `cloud-deploy` environment
- **THEN** GitHub refuses the environment, and the job receives no deploy token

#### Scenario: The workflow tries to reach the cluster

- **WHEN** the workflow's job runs
- **THEN** it holds no kubeconfig and no private-network key, and it calls only Substrate's HTTPS admin route

### Requirement: The release automation never clears a rollout pause

Substrate's deploy principal SHALL be able to read the release view and to set `cellImage`, and nothing else. Clearing a rollout pause and setting one cell's image SHALL need the admin token. While the rollout is paused, the workflow SHALL set nothing, and SHALL fail with the pause's error code and held cell.

#### Scenario: The deploy principal tries to clear a pause

- **WHEN** a request with the deploy token sends `clearRolloutPause`
- **THEN** Substrate refuses it, and the rollout stays paused

#### Scenario: The deploy principal tries to set one cell's image

- **WHEN** a request with the deploy token sends `cellId` and `cellDesiredImage`
- **THEN** Substrate refuses it and writes nothing

#### Scenario: The workflow finds the rollout paused

- **WHEN** the workflow reads a paused rollout before it sets the image
- **THEN** it sets nothing, and fails with the pause's error code and held cell

#### Scenario: The rollout pauses while the workflow polls

- **WHEN** the rollout pauses before `last_good_image` equals the target
- **THEN** the workflow fails with the pause's error code, and the pause stays until a person clears it

### Requirement: Cells move only when the platform carries the release

The workflow SHALL set `cell_image` only when the newest successful platform Deployment names a release whose `infra/helm/platform/**` and `infra/cellctl/**` equal the new release's. Otherwise, or with no platform Deployment, it SHALL set nothing and raise `cloud-release-platform-needed` through the alert receiver. A successful platform Deployment SHALL start the workflow again, and SHALL resolve the alert when the gate then passes.

#### Scenario: The release changes no platform file

- **WHEN** the new release and the platform Deployment's release hold the same chart and controller files
- **THEN** the workflow sets `cell_image`

#### Scenario: The release changes the chart

- **WHEN** the new release changes a file under `infra/helm/platform/` since the platform Deployment's release
- **THEN** the workflow sets nothing, raises `cloud-release-platform-needed`, and fails

#### Scenario: A person upgrades the platform

- **WHEN** a platform Deployment for the new release reaches the state `success`
- **THEN** the workflow runs again, sets `cell_image`, and resolves `cloud-release-platform-needed`

#### Scenario: No platform Deployment exists

- **WHEN** the workflow runs and no successful platform Deployment exists
- **THEN** it sets nothing and raises `cloud-release-platform-needed`

### Requirement: Upgrade barriers stop the automation

The barrier file SHALL list each release that is one-way or needs an operator step, with its scope, kind and runbook section. A pull request that adds such a change SHALL add its entry. The workflow SHALL set nothing when a move crosses a `cell` entry that no platform Deployment lists as crossed. The platform upgrade command SHALL refuse to cross a `platform` entry without `--crossed`. Crossing a one-way entry SHALL never roll back automatically.

#### Scenario: A release crosses a cell barrier

- **WHEN** a `cell` entry lies between the cell Deployment's release and the new release, and no platform Deployment lists it as crossed
- **THEN** the workflow sets nothing, raises `cloud-release-barrier` with the entry's ID and runbook, and fails

#### Scenario: A person crossed the barrier

- **WHEN** a successful platform Deployment lists the entry in `barriers_crossed`
- **THEN** the workflow treats the entry as crossed and sets `cell_image`

#### Scenario: An entry names a missing runbook section

- **WHEN** a barrier entry's `runbook` names a section that does not exist
- **THEN** the barrier file's CI check fails

#### Scenario: The platform crosses a one-way barrier

- **WHEN** the platform upgrade command crosses a `platform` entry of kind `one-way`
- **THEN** it upgrades without `--atomic`, so Helm cannot roll the controller back across it

#### Scenario: A person crosses a one-way cell barrier

- **WHEN** the platform upgrade command crosses a `cell` entry of kind `one-way`
- **THEN** it adds the target digest to `cellctl.forwardOnlyImages`

### Requirement: A person upgrades the platform with one reviewed command

The platform upgrade command SHALL run in an admin session with the operator's presence. Before any change, it SHALL render and schema-check the chart, diff it against the live release, run a server dry run, probe the control database's columns, diff the secret contracts, and check `platform` barriers. It SHALL upgrade with `--atomic` unless a crossed barrier is one-way. It SHALL record the platform Deployment of the upgrade's outcome.

#### Scenario: The control database lacks a column

- **WHEN** the release's controller reads a column that the control database does not have
- **THEN** the command stops before any change and names the column

#### Scenario: A secret contract changes without a barrier

- **WHEN** the tag's secret contracts differ from the live release's, and no barrier entry covers the change
- **THEN** the command stops before any change and names the omission

#### Scenario: A clean upgrade

- **WHEN** every review step passes, and no crossed barrier is one-way
- **THEN** the command upgrades with `--atomic`, and records a successful platform Deployment for the tag

#### Scenario: The upgrade fails

- **WHEN** the upgrade does not become Ready within its timeout
- **THEN** the command records a failed platform Deployment, and the newest successful platform Deployment keeps its release

#### Scenario: The controller starts against an older schema

- **WHEN** `cellctl` starts and a column that it reads or writes is missing
- **THEN** it refuses to start and names the column

### Requirement: The controller alerts when a rollout stops

`cellctl` SHALL raise `cloud-rollout-paused`, `cloud-rollout-held`, `cloud-rollout-stalled` and `cloud-rollout-canary-waiting` through the existing alert receiver, as content-free transitions. Their thresholds SHALL come from the observability contract, and `cellctl` SHALL refuse to start without them. A pass that did not observe every cell SHALL send no transition. Neither `cellctl` nor the workflow SHALL clear a pause.

#### Scenario: The rollout pauses

- **WHEN** an attempt pauses the rollout
- **THEN** `cloud-rollout-paused` fires, and it resolves only after a person clears the pause

#### Scenario: A hold lasts too long

- **WHEN** a cell's upgrade or restore hold is older than the configured held threshold
- **THEN** `cloud-rollout-held` fires, and `cellctl`'s log names the cell

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
