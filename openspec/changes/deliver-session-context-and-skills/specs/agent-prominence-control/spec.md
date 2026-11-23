## ADDED Requirements

### Requirement: Recall is asked for per new subject
The native MCP activation nudge and the server instructions SHALL ask the agent to recall when the user's turn opens a subject from the user's work or life that the context it already holds does not cover, with `recall_context` carrying that subject as `conversation.focus` or with `search_memory` and the agent's own query, before it answers.
They SHALL NOT limit recall to one call per conversation. The nudge SHALL also say that general questions need no recall, because an unscoped rule made agents recall on general-knowledge turns and added that latency for nothing. The server instructions gate recall on the engagement level instead ("when recall is warranted").
The agent decides what counts as a new subject; no word list or detector SHALL decide it.

#### Scenario: A follow-up turn changes subject
- **WHEN** a later turn in a conversation raises a subject the held context does not cover
- **THEN** the instructions the agent holds tell it to recall for that subject before answering

#### Scenario: The first subject from the user's life comes on a later turn
- **WHEN** a conversation in a client without hooks opens with a general question, and a later turn opens a subject from the user's own life or work
- **THEN** the server instructions tell the agent to recall before it answers that later turn, and they say that the balanced and maximal levels recall on any such subject

#### Scenario: A follow-up on the same subject
- **WHEN** a later turn continues the subject the held context already covers
- **THEN** the instructions ask for no new recall

#### Scenario: A general question needs no recall
- **WHEN** a turn asks a general question that needs nothing from the user's work or life
- **THEN** the instructions ask for no recall
