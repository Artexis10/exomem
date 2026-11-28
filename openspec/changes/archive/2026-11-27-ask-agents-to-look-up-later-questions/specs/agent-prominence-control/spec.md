## ADDED Requirements

### Requirement: A hookless agent's first recall asks for a lookup on later new questions
In the conversation engagement context, at a level that recalls without being asked (`balanced` or `maximal`), an MCP agent's first `recall_context` result in a conversation SHALL carry `later_turns`. It is one sentence that asks the agent to look up a later turn that asks something new about the user's life or work, or about a topic their memory lists, and that says general questions need none. No word list or detector SHALL decide what counts.

#### Scenario: A first recall from a hookless client
- **WHEN** an MCP agent on a conversation surface such as claude.ai recalls at `balanced` with no `continuity` token
- **THEN** the result carries `later_turns`, of at most 240 characters

#### Scenario: Where the sentence is not served
- **WHEN** the call carries a `continuity` token, or comes from a hook, the CLI or REST, or from a coding surface, or the level is `off` or `light`
- **THEN** the result carries no `later_turns`

#### Scenario: The sentence names only calls the caller can make
- **WHEN** the serving surface does not let the caller call `search_memory` or the recall
- **THEN** the result carries no `later_turns`

### Requirement: The later-turns sentence fits the packet budget
The `later_turns` sentence SHALL count against the packet's character budget. The packet SHALL hold its characters back before it composes the map only when they are at most a tenth of the requested characters; otherwise the sentence SHALL be added only when room remains. A sentence that cannot be added SHALL leave `later_turns_unavailable` in `diagnostics` and SHALL NOT fail the recall.

#### Scenario: A full map at the default budget
- **WHEN** a first recall that gets the sentence asks for the default budget and the map could fill it
- **THEN** the result carries both map items and `later_turns`, within the budget

#### Scenario: A small requested packet keeps its map
- **WHEN** a first recall that gets the sentence asks for fewer than ten times its characters
- **THEN** the map is composed with the whole requested budget, and the sentence is added only if room remains

### Requirement: The shipped skill's description counts a matter under way as the user's own case
The description of the shipped skill SHALL say that a practical question about a job, a purchase or a booking under way for the user is their own case, so that an agent which sees only skill descriptions recalls before it advises on it. The description SHALL stay within 1024 characters, and the rules it gives up for that room SHALL stay in the skill body. The agent decides what counts; no word list or detector SHALL decide it.

#### Scenario: A job at the user's home in a client that shows only skill descriptions
- **WHEN** the user asks what to check before tradespeople finish a job at their home, and memory records what was agreed for it
- **THEN** the description the agent holds names a job under way for the user as their own case
