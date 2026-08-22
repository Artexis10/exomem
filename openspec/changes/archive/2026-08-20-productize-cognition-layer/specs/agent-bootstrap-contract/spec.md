# agent-bootstrap-contract

## MODIFIED Requirements

### Requirement: Generic Client Workflow Guidance
The bootstrap contract SHALL tell generic agents to use product commands for
common workflows: search before answering project or durable-knowledge
questions, treat misses as scoped misses, prefer compiled notes for conclusions,
use raw sources/evidence for provenance, and save durable conclusions as
compiled knowledge. It SHALL also teach the product
distinction between built-in AI memory and Exomem: built-in memory is for user
preferences, working rules, and routing instructions; Exomem is for durable
governed knowledge with sources, proof, history, decisions, records, and review.

#### Scenario: Bootstrap teaches the product command loop
- **WHEN** an agent reads the bootstrap response
- **THEN** it can identify the recommended loop from initial `ask_memory`
  through optional `read_memory` or packed context, reasoning in the agent, and
  `remember`, `edit_memory`, or `replace_memory` for durable conclusions
- **AND** it can identify product command defaults for normal, reasoning, and
  diagnostics lookup

#### Scenario: Bootstrap teaches the core workflow
- **WHEN** an agent reads the bootstrap response
- **THEN** it can identify the recommended loop from initial lookup through
  optional `get`/`pack`, reasoning, and `note`/`edit`/`replace`
- **AND** it can identify the normal, reasoning, and diagnostics `find`
  defaults
- **AND** it can identify when to use built-in model memory versus Exomem

#### Scenario: Bootstrap maps advanced concepts to product commands
- **WHEN** the bootstrap response mentions an advanced capability such as graph
  enrichment, evidence transfer, compile planning, audit fixing, reconciliation,
  media frames, or tier-2 file management
- **THEN** it names the product command that exposes that capability
- **AND** it does not require the agent to call canonical primitive tools by
  default

## ADDED Requirements

### Requirement: Bootstrap Presents Simple Front-Door Actions
The bootstrap contract SHALL present the primary user/agent actions as save,
adopt/import, ask, prove, review, update, and connect. For each action it SHALL
name the preferred tool or composition of tools and the internal typed
operation(s) that enforce governance. It SHALL keep advanced tools visible but
secondary.

#### Scenario: Agent can route a proof request
- **WHEN** an agent reads the bootstrap response and the user asks "prove this"
  or "save this for my warranty case"
- **THEN** the agent can identify the evidence/proof workflow
- **AND** it can distinguish that workflow from ordinary source capture

#### Scenario: Agent can route an existing-vault request
- **WHEN** an agent reads the bootstrap response and the user asks to import or
  adopt an old vault
- **THEN** the agent can identify the scan-first adoption workflow
- **AND** it knows existing files are read-only by default
