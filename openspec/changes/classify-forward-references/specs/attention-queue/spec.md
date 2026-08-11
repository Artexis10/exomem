## ADDED Requirements

### Requirement: Forward references are distinct from broken wikilinks

The audit surface SHALL classify an unresolved Markdown-page wikilink as an informational `forward_reference`, not as a `broken_wikilink`. It SHALL continue to classify definite resolution errors such as a missing explicit-extension attachment or an ambiguous note title as `broken_wikilink`. Both categories SHALL be independently filterable, and a default audit SHALL register the forward-reference category.

This reporting distinction SHALL remain read-only and SHALL NOT make an unresolved or ambiguous target satisfy the semantic relation-disposition connectivity lane.

#### Scenario: Missing note is a forward reference

- **WHEN** a page links to a Markdown-page target that does not exist
- **THEN** audit emits an informational `forward_reference` finding for the link
- **AND** audit does not emit a `broken_wikilink` finding for that link
- **AND** the page's semantic relation disposition remains unsatisfied by that link

#### Scenario: Creating the target clears the finding

- **WHEN** a page with a forward-reference finding is created at the referenced target
- **THEN** the next audit resolves the wikilink and emits no finding for it

#### Scenario: Definite resolution error stays broken

- **WHEN** a wikilink names a missing explicit-extension attachment or resolves ambiguously
- **THEN** audit emits `broken_wikilink` rather than `forward_reference`
