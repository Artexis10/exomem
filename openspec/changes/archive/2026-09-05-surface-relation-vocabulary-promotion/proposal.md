## Why

Authors can currently write an unregistered relation label without receiving an immediate route to govern it, while corpus inference exposes counts but proposes only a narrow subset of unknown labels. That leaves recurring vocabulary visible but unnecessarily hard to promote.

## What Changes

- Treat an unregistered relation observation as advisory at write time while keeping it ineligible for both typed-edge and connectivity qualification.
- Add a non-blocking public write-feedback signal for unregistered relation rows, including the exact labels and the governed promotion route.
- Add a matching next action without changing the top-level note response shape.
- Populate the inferred relation-registry proposal for unregistered labels observed at least three times by default, leaving parent and description unset for human review.
- Keep inference read-only and keep registry persistence behind the existing explicit `save=true` and expected-hash guards.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `command-surface`: Compiled-note writes report unregistered relation vocabulary inside existing write feedback and name the promotion command.
- `attention-queue`: Recurring unregistered relation observations become proposal-ready review work without automatic registry mutation.
- `semantic-write-contract`: Unregistered relation observations are warnings rather than independent blockers, but never satisfy relation disposition.

## Impact

The semantic relation-registry finding severity, compiled-note feedback builder, deterministic relation-registry inference, and their focused tests change. No operation docstring, command parameter, save guard, gate threshold, or external dependency changes.
