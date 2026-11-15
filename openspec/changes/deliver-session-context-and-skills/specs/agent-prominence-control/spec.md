## ADDED Requirements

### Requirement: Recall is asked for per new subject
The native MCP activation nudge and the server instructions SHALL ask the agent to recall when the user's turn opens a subject that the context it already holds does not cover, with `recall_context` carrying that subject as `conversation.focus` or with `search_memory` and the agent's own query, before it answers.
They SHALL NOT limit recall to one call per conversation.
The agent decides what counts as a new subject; no word list or detector SHALL decide it.

#### Scenario: A follow-up turn changes subject
- **WHEN** a later turn in a conversation raises a subject the held context does not cover
- **THEN** the instructions the agent holds tell it to recall for that subject before answering

#### Scenario: A follow-up on the same subject
- **WHEN** a later turn continues the subject the held context already covers
- **THEN** the instructions ask for no new recall
