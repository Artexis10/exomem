## ADDED Requirements

### Requirement: Unified context exposes registry-aware traversal
Unified context SHALL accept a traversal profile and return the resolved profile,
core registry version, extension-registry hash, included relation families,
canonical/raw relation metadata, unknown/out-of-scope counts, warnings, and
explicit truncation. Extension edges selected through a core parent SHALL retain
their more precise canonical key in the response.

#### Scenario: Cross-domain support remains precise and portable
- **WHEN** epistemic context encounters a registered domain extension whose core
  parent is `supports`
- **THEN** the edge is included as its namespaced canonical type, reports parent
  `supports`, and carries its raw label and source provenance

### Requirement: Context never hides unknown relation observations
Normal context SHALL exclude unregistered edges from traversal but SHALL report
their bounded count and source examples as advisory warnings when they occur in
the selected neighborhood. An explicit diagnostic view MAY include the
semantically inert observed edges while clearly marking them unregistered. These
warnings MUST NOT add items to default attention or alter ordinary retrieval.

#### Scenario: Unknown edge is warned without semantic promotion
- **WHEN** a seed page contains a valid but unregistered typed relation
- **THEN** context reports the observation in warnings and does not treat it as a
  core or extension family edge
