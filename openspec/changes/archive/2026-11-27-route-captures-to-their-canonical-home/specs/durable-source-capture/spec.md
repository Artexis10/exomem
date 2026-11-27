## ADDED Requirements

### Requirement: A committed Source save names how to apply the file

A committed `save_source` reply SHALL carry a `next_action` telling the agent that, if the person presents the file as the current state, it updates the affected note's current statement in the same turn, cites the Source, and keeps the earlier value as dated history. The reply SHALL NOT state that the file's claims are verified.

#### Scenario: A handed applied export is applied to its note

- **WHEN** an agent saves a file the person calls the final or applied export
- **THEN** the reply's `next_action` tells it to update the note's current statement and cite the Source

#### Scenario: A custody-only save is not told to edit

- **WHEN** the save uses `options.custody_only`
- **THEN** the reply carries no instruction to update a note
