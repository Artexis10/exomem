## ADDED Requirements

### Requirement: Adoption Studio Is Admitted Generically, Not Intercepted

The hosted command route SHALL admit `adoption_studio` through its generic dispatch path, classifying read versus mutation solely by `commands_module.invocation_is_read_only(command, kwargs)` and routing to `lifecycle.admit_read()` or `admit_mutation()` accordingly. `adoption_studio` SHALL NOT be added to the hosted intercept set (which remains scoped to `transfer_artifact` and `adopt_vault`). Vault-relative path confinement (`resolve_under_vault`) plus the run state machine SHALL be the safety layer, so no hosted-route change is required for adoption command flow and the command's read/write behavior stays consistent with its cell MCP and REST surfaces.

#### Scenario: A mutating adoption action is admitted as a mutation

- **WHEN** the gateway forwards `adoption_studio` with a mutating action (for example `apply`) to a cell
- **THEN** the generic hosted route classifies it via `invocation_is_read_only` and admits it through `admit_mutation()`
- **AND** `adoption_studio` never enters the hosted intercept set

#### Scenario: A read-only adoption action is admitted as a read

- **WHEN** the gateway forwards `adoption_studio` with `status` or `work-item`
- **THEN** the generic hosted route admits it through `admit_read()` without acquiring the mutation boundary

### Requirement: Adoption Uploads Land In Vault-Relative Per-Run Staging

Hosted adoption intake SHALL land uploaded files and expanded archive entries as RAW files under the vault-relative per-run directory `_Staging/adoption/<run_id>/`, outside `Knowledge Base/`, so the engine scans them as legacy input rather than governed content. ZIP archives SHALL be expanded cell-side with zip-slip protection confining every extracted path under the staging directory, and with enforced entry-count and total-size caps. A subsequent `adoption_studio(action="start", path="_Staging/adoption/<run_id>")` SHALL scan the staged material through the same engine used for a local folder, and the intake SHALL return a poll-shaped result Home can consume without cell master credentials or private addresses.

#### Scenario: Staged upload is scannable by the same engine

- **WHEN** files are uploaded for a hosted adoption run and land under `_Staging/adoption/<run_id>/`
- **THEN** `adoption_studio(start, path="_Staging/adoption/<run_id>")` scans them identically to a local subtree
- **AND** the staged files are outside `Knowledge Base/` and no governed Source is created by the intake itself

#### Scenario: Malicious archive entries are rejected

- **WHEN** an uploaded ZIP contains a traversal entry or exceeds the entry-count or total-size cap
- **THEN** the traversal entry is rejected and the caps are enforced, with every accepted entry confined under `_Staging/adoption/<run_id>/`
