## ADDED Requirements

### Requirement: Bootstrap teaches the governed relation vocabulary loop

Every bootstrap profile SHALL teach agents to resolve relation intent before inventing a label, inspect continuation/inventory guidance when candidate evidence is truncated, prefer the most specific truthful active relation, preserve `relates_to` and no edge as honest outcomes, propose a vault extension only for a durable or recurring semantic need, and save only a reviewed complete proposal with an audit reason and current registry hash. It SHALL teach that an existing canonical meaning is evolved through a new key plus deprecation/replacement rather than an in-place semantic edit. Compact bootstrap SHALL name the read-only resolve route and governed proposal/save routes plus the portable core keys; richer profiles MAY add generic examples. The guidance MUST NOT imply that similarity proves semantic equivalence or that the server chooses or authors a relation.

#### Scenario: A fresh agent can discover relation evolution from compact bootstrap
- **WHEN** a generic client requests the default compact bootstrap
- **THEN** it learns the resolve, reuse, honest-fallback, propose, and guarded-save sequence using only exported operations
- **AND** it learns that vault canonical keys may be namespaced while clean aliases are valid authoring labels

#### Scenario: Bootstrap keeps `relates_to` legitimate
- **WHEN** an agent reads relation-authoring guidance
- **THEN** the guidance tells it not to invent false specificity and not to propose vocabulary merely to improve a graph metric

#### Scenario: Generic scaffold contains no private ontology
- **WHEN** the shipped scaffold and workflow skills are inspected
- **THEN** their relation examples use only synthetic generic domains and labels
- **AND** they contain no private or user-specific identifier, path, project, or vault-derived example

### Requirement: Bootstrap exposes relation registry currency without scanning content

Bootstrap SHALL expose the active relation contract version, core registry version, extension registry hash, extension count, and the route for bounded relation inventory. It MUST obtain this information from registry metadata without reading note bodies. Vault extension definitions MAY be returned only by the authenticated resolver or explicit schema inventory for the addressed vault.

#### Scenario: Compact bootstrap remains bounded with many extensions
- **WHEN** a vault contains more extensions than the compact response budget permits
- **THEN** bootstrap reports the count, hash, and inventory route without embedding the unbounded registry
- **AND** the resolver can return bounded candidates for a requested intent
