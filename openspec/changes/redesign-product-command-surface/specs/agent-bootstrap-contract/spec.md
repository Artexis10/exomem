## MODIFIED Requirements

### Requirement: Agent Bootstrap Contract
The system SHALL expose a read-only `bootstrap` operation that returns a
versioned operating contract for agents using Exomem without a native skill.
The contract MUST be deterministic, structured JSON and MUST NOT inspect or
summarize private vault content. The contract SHALL present product commands as
the default public interface and identify canonical leaves only as internal or
advanced implementation concepts where needed.

#### Scenario: Compact bootstrap returns the operating contract
- **WHEN** `bootstrap` is called with default arguments
- **THEN** the response includes `contract_version`, `server`, `workflow`,
  `product_commands`, `tool_defaults`, `performance_profiles`,
  `search_guidance`, and `common_tools`
- **AND** the response identifies the current compute policy
- **AND** the response does not include note bodies, excerpts, paths from the
  user's vault contents, or private project names

#### Scenario: Invalid bootstrap profile is rejected
- **WHEN** `bootstrap(profile="invalid")` is called
- **THEN** the operation fails with a validation error naming the accepted
  profiles

### Requirement: Generic Client Workflow Guidance
The bootstrap contract SHALL tell generic agents to use product commands for
common workflows: search before answering project or durable-knowledge
questions, treat misses as scoped misses, prefer compiled notes for conclusions,
use raw sources/evidence for provenance, and save durable conclusions as
compiled knowledge.

#### Scenario: Bootstrap teaches the product command loop
- **WHEN** an agent reads the bootstrap response
- **THEN** it can identify the recommended loop from initial `ask_memory`
  through optional `read_memory` or packed context, reasoning in the agent, and
  `remember`, `edit_memory`, or `replace_memory` for durable conclusions
- **AND** it can identify product command defaults for normal, reasoning, and
  diagnostics lookup

#### Scenario: Bootstrap maps advanced concepts to product commands
- **WHEN** the bootstrap response mentions an advanced capability such as graph
  enrichment, evidence transfer, compile planning, audit fixing, reconciliation,
  media frames, or tier-2 file management
- **THEN** it names the product command that exposes that capability
- **AND** it does not require the agent to call canonical primitive tools by
  default