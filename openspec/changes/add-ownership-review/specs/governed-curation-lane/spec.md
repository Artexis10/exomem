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

### Requirement: A page-level review item opens a work item on its page

A curation work item opened with the `review_ref` of a review item that is not a recurring-identity candidate SHALL assemble the context of that item's page, bounded as a work item opened with that page's ref is. A recurring-identity candidate's `review_ref` SHALL keep its own binding and continuation. A `review_ref` that resolves to no current item SHALL keep its current refusal. The work item SHALL NOT widen what curation may read or write.

#### Scenario: An ownership item opens a work item on its page

- **WHEN** a client opens a curation work item with the `review_ref` of an open `ownership_review` item
- **THEN** the work item holds that item's page with its content and content hash
- **AND** the call does not refuse the ref as a recurring-identity candidate

#### Scenario: A recurring-identity ref keeps its binding

- **WHEN** a client opens a curation work item with the `review_ref` of a recurring-identity candidate
- **THEN** the work item carries the candidate binding and its allowed step kinds, as before
