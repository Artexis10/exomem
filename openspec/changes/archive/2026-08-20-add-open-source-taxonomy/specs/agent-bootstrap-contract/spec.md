## ADDED Requirements

### Requirement: Bootstrap teaches that source classification vocabularies are open

Bootstrap SHALL tell a generic agent that source kind and subject domain are open, extensible vocabularies rather than closed enumerations, that a meaningful new label is accepted without a release, and that project association is a separate multi-valued axis.

Where bootstrap names example labels it SHALL frame them as non-exhaustive defaults, so an agent does not read the shipped set as the permitted set. Example labels SHALL be generic and SHALL NOT include any user-specific project, client, or organisation identifier.

Bootstrap SHALL NOT publish the fallback kind as the default argument for capture.

#### Scenario: The contract states the vocabularies are open

- **WHEN** an agent reads the compact bootstrap contract
- **THEN** it learns that source kind and subject domain are open and extensible
- **AND** it learns that project association is separate and may name more than one project
- **AND** any labels it is shown are marked as non-exhaustive

#### Scenario: The fallback is not advertised as the capture default

- **WHEN** an agent reads the capture routing guidance in bootstrap
- **THEN** that guidance does not instruct it to pass the fallback kind

#### Scenario: Published example labels carry no user-specific identifier

- **WHEN** the bootstrap payload is inspected for classification guidance
- **THEN** every example kind, domain, and project label is generic

### Requirement: Bootstrap teaches how to treat the fallback and a classification suggestion

Bootstrap SHALL tell an agent to classify a source semantically when it can, to use the fallback kind only when classification genuinely cannot be determined, and specifically not to use the fallback merely because no built-in label matches.

Bootstrap SHALL tell an agent to inspect an advisory classification suggestion returned after a capture, to surface a strong one in the user's own domain language rather than in product-internal terms, and to exercise judgement on a weaker one rather than repeating advice.

This guidance SHALL fit within the existing compact-profile size budget.

#### Scenario: The contract distinguishes low confidence from missing vocabulary

- **WHEN** an agent reads the source-capture guidance
- **THEN** it learns to name the kind it believes is correct even when that label is unfamiliar to the product
- **AND** it learns that the fallback means low confidence, not absent vocabulary

#### Scenario: The contract teaches suggestion handling

- **WHEN** an agent reads the post-write guidance
- **THEN** it learns to inspect a returned classification suggestion
- **AND** it learns to present a strong one in domain language
- **AND** it learns not to repeat the same advice within one interaction

#### Scenario: Compact guidance stays within budget

- **WHEN** the compact bootstrap payload is produced with this guidance present
- **THEN** its serialized size remains within the established compact ceiling

### Requirement: Bootstrap exposes configured classification vocabulary without becoming a second ontology

Bootstrap SHALL be able to surface the classification labels a selected knowledge pack makes discoverable, so a configured agent sees relevant vocabulary immediately.

Pack-surfaced labels SHALL be advisory discovery hints that resolve against the same source-taxonomy vocabulary. A pack SHALL NOT define a competing classification model, and selecting a pack SHALL NOT create, reserve, or require any label.

Classification SHALL function fully with no pack selected.

#### Scenario: A selected pack surfaces relevant labels

- **WHEN** a pack is selected and bootstrap is requested at a profile that carries pack detail
- **THEN** the payload can present that pack's suggested kinds and domains
- **AND** those labels resolve against the same source-taxonomy vocabulary

#### Scenario: Pack selection creates nothing

- **WHEN** a pack that suggests classification labels is selected
- **THEN** no registry entry, directory, or reservation is created for those labels

#### Scenario: Classification works with no pack selected

- **WHEN** no pack has been selected
- **THEN** source kind and domain classification, projection, and retrieval all still function
