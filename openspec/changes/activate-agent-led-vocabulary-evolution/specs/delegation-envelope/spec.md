## ADDED Requirements

### Requirement: Explicit v2 additive authority does not raise a v1 ceiling

The v1 action classes, ranges, hard ceilings and standing-delegation refusal SHALL remain unchanged for v1 vaults. The deliberately activated v2 additive-authority contract SHALL supply separate action-specific grants for additive entity, entity-type, relation-type and edge operations; it SHALL NOT be represented as a non-confirm `restructure_execution` disposition or a more permissive prominence setting.

In an activated v2 vault the canonical effect classifier SHALL route those additive effects through `scoped-additive-authority` on every adapter. All remaining restructuring and disclosure effects SHALL retain their existing authority owner and gates. An enclosing confirmed curation plan SHALL NOT broaden a leaf grant, and a leaf grant SHALL NOT waive confirmation for a mixed or destructive curation plan. Read-time tolerance of unknown v1 configuration SHALL never be used as write-time fallback from an activated v2 contract.

#### Scenario: A grant covers only its named additive effect

- **WHEN** a v2 user delegates entity-instance creation
- **THEN** that grant can authorize a matching new entity through the v2 leaf gate
- **AND** it cannot authorize entity merge, supersession, existing-fact replacement or deletion

#### Scenario: Standing restructuring delegation is still refused

- **WHEN** either a v1 or v2 caller sets `restructure_execution` to silent
- **THEN** the existing founder-gate refusal remains unchanged; v2 additions use their separate user-grant contract instead

#### Scenario: Mixed curation retains the stricter requirement

- **WHEN** a curation plan includes a delegated edge addition and an existing-page rewrite
- **THEN** the edge grant alone cannot authorize the enclosing plan or rewrite
