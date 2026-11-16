## ADDED Requirements

### Requirement: Recall is asked for per new subject
The native MCP activation nudge and the server instructions SHALL ask the agent to recall when the user's turn opens a subject of the user's own (their work, projects, records or life) that the context it already holds does not cover, with `recall_context` carrying that subject as `conversation.focus` or with `search_memory` and the agent's own query, before it answers.
They SHALL NOT limit recall to one call per conversation. The nudge SHALL also say that a general question needing nothing of the user's own needs no recall, because an unscoped rule made agents recall on general-knowledge turns and added that latency for nothing; the server instructions already ask for recall only when it is warranted.
The agent decides what counts as a new subject; no word list or detector SHALL decide it.

#### Scenario: A follow-up turn changes subject
- **WHEN** a later turn in a conversation raises a subject the held context does not cover
- **THEN** the instructions the agent holds tell it to recall for that subject before answering

#### Scenario: A follow-up on the same subject
- **WHEN** a later turn continues the subject the held context already covers
- **THEN** the instructions ask for no new recall

#### Scenario: A general question needs no recall
- **WHEN** a turn asks a general question that needs nothing of the user's own
- **THEN** the instructions ask for no recall
