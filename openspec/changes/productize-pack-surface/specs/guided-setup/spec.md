# guided-setup

## MODIFIED Requirements

### Requirement: Setup Teaches The Cognition Layer
The setup summary and first-run docs SHALL explain the simple Exomem model:
built-in AI memory stores preferences/routing; Exomem stores durable governed
knowledge with sources, proof, history, decisions, records, and review. The
summary SHALL include first prompts that use simple verbs rather than internal
ontology. Setup SHALL also present knowledge packs as beginner-facing product
choices and persist selected packs under the governed Knowledge Base layer.

#### Scenario: Fresh vault chooses useful packs
- **WHEN** setup runs against a fresh or structurally empty vault
- **THEN** it presents available packs with beginner-facing descriptions
- **AND** non-interactive setup persists the default personal-records pack
- **AND** interactive setup can persist multiple selected packs

#### Scenario: Existing vault confirms inferred packs
- **WHEN** setup detects existing non-KB content and adoption suggests packs
- **THEN** setup shows the suggested packs as choices rather than a migration
- **AND** persisted selection records only guidance metadata under
  `Knowledge Base/`

#### Scenario: First prompts are simple
- **WHEN** setup finishes successfully
- **THEN** the printed next steps include prompts such as "what does this vault
  look like?", "import/adopt my old notes safely", "what do we know about X?",
  "show the sources", and "what needs review?"
- **AND** the prompt examples do not require the user to know internal page
  types