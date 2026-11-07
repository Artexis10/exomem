## ADDED Requirements

### Requirement: The agent contract teaches the note-cohesion question

Bootstrap and the canonical write loop SHALL teach agents that a compiled write may return one `structure_suggestion` with `kind` `note_cohesion`, and SHALL state how to answer it.

The guidance SHALL direct the agent to read the whole page and name the independent questions it answers. It SHALL keep a page that answers one question whole, however long, by dismissing the item through `triage_memory` with an `intentional:` reason. It SHALL propose a split only when several questions each have their own material, and only through the curation lane with the user's exact plan confirmation. It SHALL add a breadth tag through a governed edit when the page is a hub by design. The guidance SHALL state that the question asserts nothing about the page and that the runtime never reorganises knowledge on its own.

#### Scenario: Bootstrap names the question and the three answers

- **WHEN** a generic client reads the bootstrap post-write guidance
- **THEN** it learns that a compiled write may carry a note-cohesion question and where to find it
- **AND** it learns to keep a coherent page whole by dismissal, to split only through the curation lane, and to mark a hub with a breadth tag

#### Scenario: Guidance is presentational, not executable

- **WHEN** bootstrap describes the note-cohesion question
- **THEN** reading bootstrap creates, moves, or reorganises nothing
- **AND** no new tool or parameter is required to receive or answer the question

## REMOVED Requirements

### Requirement: The agent contract teaches structural-promotion presentation

**Reason**: The structural-promotion suggestion it taught is removed from `command-surface`.

**Migration**: Replaced by "The agent contract teaches the note-cohesion question".
