## MODIFIED Requirements

### Requirement: A hookless agent's first recall asks for a lookup on later new questions
In the conversation engagement context, at a level that recalls without being asked (`balanced` or `maximal`), an MCP agent's first `recall_context` result in a conversation SHALL carry `later_turns`. It is one sentence that asks the agent to search memory before it answers a later turn with a new question or claim on a subject the user's memory lists, even when the question sounds like general knowledge or comes from someone else. No word list or detector SHALL decide what counts.

#### Scenario: A first recall from a hookless client
- **WHEN** an MCP agent on a conversation surface such as claude.ai recalls at `balanced` with no `continuity` token
- **THEN** the result carries `later_turns`, of at most 240 characters

#### Scenario: Where the sentence is not served
- **WHEN** the call carries a `continuity` token, or comes from a hook, the CLI or REST, or from a coding surface, or the level is `off` or `light`
- **THEN** the result carries no `later_turns`

#### Scenario: The sentence names only calls the caller can make
- **WHEN** the serving surface does not let the caller call `search_memory` or the recall
- **THEN** the result carries no `later_turns`

#### Scenario: A claim that sounds general still gets a lookup
- **WHEN** a later turn brings a claim on a subject the memory lists that the agent could answer from general knowledge
- **THEN** the sentence asks for a search before the answer, and offers no exit for questions the agent judges general
