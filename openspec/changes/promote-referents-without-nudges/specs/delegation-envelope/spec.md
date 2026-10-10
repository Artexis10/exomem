## ADDED Requirements

### Requirement: An owner's additive promotions follow proactive capture

When the caller resolves to the vault owner and the vault has not activated v2 additive authority, these promotions SHALL be `proactive_capture` and SHALL follow its served disposition:
an entity-type or semantic-category save, a relation-extension save, an entity creation, and a typed edge that the agent authors.
The served contract SHALL state this rule and SHALL NOT tell such a caller that vocabulary writers need confirmation.
Acceptance of a relation that the server's queue suggested SHALL remain `link_acceptance`.
Merge, supersession, deletion, meaning changes, alias edits on existing entries and deprecation SHALL remain `restructure_execution`.
A resolved non-owner's registry save SHALL remain a pending item for the owner, and its entity and edge creation SHALL stay inside its existing write scope.
In a vault that activated v2, every such effect SHALL pass the v2 effect classifier.
No upgrade, migration or default SHALL activate v2 or route a v1 owner's promotion to an approval step.
This requirement supersedes the confirm-required classification of an unknown-kind entity-type save in the requirement "Entity lifecycle actions retain existing authority classes".

#### Scenario: A type and an edge are saved without a question

- **WHEN** prominence is `balanced`, the owner's agent finds no fitting entity type, saves one under a parent, and authors an `owns` edge to an entity of that type
- **THEN** both writes follow `proactive_capture` and the agent asks no confirmation question
- **AND** each write is surfaced once in the next session and carries a one-call revert route

#### Scenario: Queue acceptance keeps its boundary

- **WHEN** the agent accepts a relation that the relation queue suggested
- **THEN** the action remains `link_acceptance` with its confirmation

#### Scenario: A restricted non-owner's save waits for the owner

- **WHEN** a resolved non-owner saves a new relation extension
- **THEN** the registry is unchanged and a pending item carries the delta for the owner

#### Scenario: An activated v2 vault keeps its gate

- **WHEN** a v2-activated vault receives a referent declaration that would create an entity, and no grant covers that effect
- **THEN** the entity creation is refused by the v2 gate and the primary write still commits

#### Scenario: An upgrade grants and gates nothing

- **WHEN** a runtime that ships owner approvals is installed on a v1 vault
- **THEN** the vault stays v1, and an owner's promotion still takes effect without an approval step
