## ADDED Requirements

### Requirement: A custody Source is listed only by name

The `custody_source` family SHALL be registered as an attention category, so a client lists it by naming it. It SHALL NOT join the default attention set or the due-state projection, so neither recall nor a write reply pushes a custody Source. Its findings SHALL stay at `info` severity. An audit or attention request that names only `custody_source` SHALL return no finding of another family.

#### Scenario: A custody Source is never pushed

- **WHEN** a Source marked `custody_only: true` has no live page compiling it
- **THEN** the default attention view and the served due-state block do not list it
- **AND** an attention request that names `custody_source` lists it at `info` severity

#### Scenario: A request for the custody count returns only that family

- **WHEN** a vault holds one custody Source and one ordinary Source that no note compiles, and a caller asks the audit for `custody_source`
- **THEN** the reply lists only the custody Source and counts no `unprocessed_source`
