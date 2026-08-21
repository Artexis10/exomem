## ADDED Requirements

### Requirement: Platform release adoption does not mutate an existing cell

Adopting a new Hosted platform release SHALL NOT itself upgrade, migrate, restart, re-provision, replace, restore, discard, destroy, relabel, or change the routing identity of an existing tenant cell. It MUST NOT modify that cell's namespace, workload, persistent volume or claim, canonical vault bytes, derived state, security state, binding, credential set, entitlement, OAuth grants, or lifecycle generation. An existing-cell release change SHALL require a separate explicit rollforward contract and fenced lifecycle operation.

#### Scenario: New default is deployed beside an existing cell

- **WHEN** a platform deployment selects a newer runtime for future provisioning while an existing tenant cell is ready
- **THEN** the existing cell continues on its prior assigned runtime and contract
- **AND** no lifecycle operation or Kubernetes mutation targets that cell

#### Scenario: Existing cell would be excluded by contract mode

- **WHEN** an operator requests contract mode while the routable census contains the existing cell's legacy release
- **THEN** the cutover is refused before admission changes
- **AND** the cell remains routable under the expand lock

#### Scenario: Existing cell is intentionally rolled forward later

- **WHEN** an operator separately authorizes an existing-cell release change
- **THEN** that change is governed by the dedicated rollforward specification, including data-preservation and exact-runtime confirmation
- **AND** platform release adoption alone supplies no implicit authority to perform it
