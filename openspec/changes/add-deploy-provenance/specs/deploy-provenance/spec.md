## ADDED Requirements

### Requirement: Runtime Reports Its Install Origin

The server SHALL report how the running code was installed, so an operator can determine the
deployed version and its origin without inspecting service-manager configuration. The report
SHALL distinguish an editable checkout install from an installed wheel, and SHALL include the
git revision when and only when the install is editable and a revision is resolvable.

#### Scenario: Wheel-backed service is identifiable as not-the-checkout

- **WHEN** `/health` is requested against a server running from an installed wheel
- **THEN** the response includes `install_source` of `wheel`
- **AND** `revision` is null
- **AND** the operator can conclude the running code is not the local checkout

#### Scenario: Editable install reports its revision

- **WHEN** `/health` is requested against a server running from an editable checkout
- **THEN** the response includes `install_source` of `editable`
- **AND** `revision` is the short git revision of that checkout when resolvable

#### Scenario: Provenance never fails the probe

- **WHEN** any provenance field cannot be determined
- **THEN** that field reports a null or `unknown` value
- **AND** `/health` still returns 200 with `status` of `ok`

### Requirement: Public Health Route Excludes Host-Identifying Detail

The `/health` route is unauthenticated and publicly reachable. It SHALL NOT expose absolute
filesystem paths, operating-system usernames, or host directory layout. Full provenance
including the interpreter path SHALL be available only through the local CLI surface.

#### Scenario: Public payload omits paths

- **WHEN** `/health` is requested
- **THEN** the response contains no absolute filesystem path
- **AND** no field reveals the operating-system username

#### Scenario: Local CLI exposes full detail

- **WHEN** an operator runs the `install-info` command locally
- **THEN** the output includes the interpreter path and the resolved package location
- **AND** the output includes every field published on `/health`

### Requirement: Torch Build Tag Reported Without Importing Torch

Provenance SHALL report the installed torch wheel build tag using distribution metadata and
MUST NOT import torch, so the probe stays fast and cannot fail on a broken ML stack. The
reported value SHALL preserve any local version tag that distinguishes an accelerated wheel
from a default CPU wheel.

#### Scenario: CUDA wheel is distinguishable from CPU wheel

- **WHEN** the environment has torch installed with a CUDA local version tag
- **THEN** provenance reports the full build tag including that local tag
- **AND** reports `accelerated` as true

#### Scenario: Default PyPI wheel reports unaccelerated

- **WHEN** the environment has a torch wheel with no accelerator local version tag
- **THEN** provenance reports `accelerated` as false

#### Scenario: Absent torch does not error

- **WHEN** torch is not installed
- **THEN** provenance reports a null torch build and `accelerated` as false

### Requirement: Deploy Resolves The Service Interpreter From The Service Manager

The deploy script SHALL resolve the target interpreter from the service manager configuration
rather than from the current working directory or an assumed checkout path. If the resolved
interpreter does not exist, the deploy SHALL abort rather than fall back to a guessed target.

#### Scenario: Deploy targets the real service venv

- **WHEN** the deploy script runs while the operator's shell is in a different checkout
- **THEN** it resolves the interpreter from the service manager
- **AND** it upgrades that interpreter's environment, not the checkout's

#### Scenario: Missing interpreter aborts the deploy

- **WHEN** the configured service interpreter path does not exist
- **THEN** the deploy aborts with an error naming the resolved path
- **AND** no upgrade or restart is attempted

### Requirement: Accelerator Capability Regression Blocks The Deploy

The deploy SHALL fail when the target environment had an accelerated torch build before an
upgrade and does not have one after it, and SHALL report the repair command. Hosts that are
intentionally CPU-only SHALL be able to acknowledge this explicitly and proceed.

#### Scenario: Silent CUDA loss fails the deploy

- **WHEN** an upgrade replaces an accelerated torch build with a default CPU build
- **THEN** the deploy fails
- **AND** the output names the accelerator index required to restore the pinned wheel

#### Scenario: CPU-only host proceeds with acknowledgement

- **WHEN** the operator passes the CPU-torch acknowledgement flag
- **THEN** the deploy proceeds despite an unaccelerated torch build

### Requirement: Deploy Verifies The Running Version

The deploy SHALL NOT report success based on installer output alone. It SHALL poll the health
route after restart and confirm the running server reports the requested version, failing if
the version does not match within a bounded wait.

#### Scenario: Restart that did not take effect fails the deploy

- **WHEN** the upgrade succeeds but the running process still serves the previous version
- **THEN** the deploy fails and reports both the requested and the observed version

#### Scenario: Successful deploy reports observed version

- **WHEN** the running server reports the requested version after restart
- **THEN** the deploy reports success including the observed version and install source
