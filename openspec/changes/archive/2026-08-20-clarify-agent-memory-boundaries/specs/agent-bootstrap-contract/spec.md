## ADDED Requirements

### Requirement: Documentation Aligns With Bootstrap Contract
Agent-facing documentation SHALL align generic-client onboarding with the
existing `bootstrap()` operating contract.

#### Scenario: Generic client instructions match bootstrap behavior
- **WHEN** a generic MCP client, hosted chat client, or client without the
  Exomem skill is documented
- **THEN** the guidance tells the agent to call `bootstrap(profile="compact")`
  once at session start
- **AND** uses the existing bootstrap action model rather than inventing a
  conflicting memory workflow

#### Scenario: Diagnostics remain separate from normal lookup
- **WHEN** documentation discusses performance or retrieval behavior
- **THEN** it distinguishes normal lookup from diagnostics and does not imply
  that rerank, packed context, cold model load, or compute mode are the same
  concern
