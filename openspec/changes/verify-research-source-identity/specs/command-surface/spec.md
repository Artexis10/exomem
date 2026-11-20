## ADDED Requirements

### Requirement: A Source may state its document form

`save_source` SHALL accept an optional `document_form` whose key resolves in the `document-forms` registry.
The shipped pack SHALL hold `full-text`, `abstract`, `citation-record`, `excerpt` and `secondary-work`.
A vault MAY add forms in its schema overlay, and a valid new key SHALL register in the same atomic write that captures the Source.
The Source page SHALL record the resolved key as `document_form`.
Code SHALL NOT branch on a document-form key.
A Source without the field SHALL be presented as having no stated form.

#### Scenario: An abstract is saved when the full text is unavailable

- **WHEN** an agent saves a publisher's abstract page with `document_form: abstract`
- **THEN** the Source records `document_form: abstract`

#### Scenario: A vault adds a form

- **WHEN** an agent saves a Source with a valid unregistered form key
- **THEN** the key registers in the vault overlay in the same write

### Requirement: An uncorrected identity conflict is an audit finding

The audit SHALL report a `source_identity_conflict` finding for each Source whose recorded `identity_check.state` is `conflict` and that has no correction receipt.
The finding's proposed fix SHALL be the metadata-only correction call for that Source.
A Source with any other state, or without a recorded check, SHALL NOT be reported by this family.

#### Scenario: An agent ignored a conflict

- **WHEN** a Source records `identity_check.state: conflict` and nobody corrected it
- **THEN** the audit reports `source_identity_conflict` with the correction call

#### Scenario: The conflict was corrected

- **WHEN** that Source has a correction receipt
- **THEN** the family reports nothing for it
