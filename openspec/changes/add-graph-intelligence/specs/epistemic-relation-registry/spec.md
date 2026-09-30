## ADDED Requirements

### Requirement: The inference save guard protects registered vocabulary in use
When `infer(save=true)` persists a reviewed relation registry, its observed-deletion guard
SHALL protect exactly the vocabulary a registry save can delete: the canonical key of
every observed label that resolves to a currently registered extension, and the alias
when the observed label was that alias. Observed core labels and unregistered labels, in
any letter case, SHALL NOT block the save. A proposal that drops an extension still used
by the corpus MUST still be refused.

#### Scenario: Legacy and unregistered labels do not block
- **WHEN** the corpus holds a capitalised core row such as `Supports`, an unregistered label and an alias of a registered extension, and the reviewed proposal keeps every current extension
- **THEN** the save succeeds

#### Scenario: A used extension cannot be dropped
- **WHEN** the corpus uses a registered extension by its key or by its alias and the reviewed proposal removes that extension
- **THEN** the save is refused with an observed-deletion error and the registry stays byte-identical
