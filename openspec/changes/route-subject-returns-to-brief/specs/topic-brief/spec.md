## ADDED Requirements

### Requirement: Clients are routed to the brief for a return to a researched subject
The server's instructions, which every MCP client receives at `initialize`, SHALL tell the client that when the person asks to bring back or recap a subject they researched before, it reads that subject's brief first, follows `more` to the last page, and uses recall or search only for what the brief does not cover. The clause SHALL name the request in plain words and leave its recognition to the model; the server SHALL NOT match words in the turn to decide it. The clause SHALL ship only after an evaluation against a serving brief shows its composite lower bound above zero.

#### Scenario: A client connects
- **WHEN** an MCP client sends `initialize`
- **THEN** the server's instructions include the clause that routes a return to a researched subject to the brief

#### Scenario: The brief is warming
- **WHEN** a client follows the clause and the brief answers `index_warming`
- **THEN** the client falls back to recall and search for the subject
