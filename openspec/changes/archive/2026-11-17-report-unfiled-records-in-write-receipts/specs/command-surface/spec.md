## ADDED Requirements

### Requirement: A note's project arguments are applied as its type stores them and reported

When a caller creates a compiled note whose type stores a `projects` list and passes a single `project` with no `projects`, the write SHALL store that value as a one-item `projects` list.
The reply SHALL name each project argument that the write applied differently or ignored in `warnings`.
A project argument SHALL NOT be dropped without a warning.

#### Scenario: A failure note names one project

- **WHEN** an agent creates a failure note with `project: widget-app` and no `projects`
- **THEN** the note stores `projects: [widget-app]`
- **AND** the reply's warnings say that `project` was applied as `projects`
- **AND** records routing sees the project
