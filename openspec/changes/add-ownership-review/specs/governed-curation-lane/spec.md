## ADDED Requirements

### Requirement: A preview that creates or moves notes asks the ownership question once

A curation preview whose plan holds `create-note` or `move` steps for compiled notes SHALL carry one ownership question. The question SHALL name each such step by its step id with the project the step assigns, state the ownership test and the deferral rule from the guidance routes, and tell the agent to correct a step's project before the user confirms. The question SHALL NOT block the preview, change a step, or alter the plan's fingerprint.

#### Scenario: A split's children are each named

- **WHEN** a plan splits one note into two `create-note` steps, each with the project key `project-alpha`
- **THEN** the preview carries one ownership question that names both step ids with `project-alpha`
- **AND** the plan's fingerprint is the same as without the question

#### Scenario: A plan without note placement carries no question

- **WHEN** a plan holds only `edit` steps
- **THEN** the preview carries no ownership question
