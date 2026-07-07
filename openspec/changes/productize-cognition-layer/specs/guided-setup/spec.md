# guided-setup

## MODIFIED Requirements

### Requirement: One-command guided local setup
The system SHALL provide an `exomem setup` CLI subcommand that performs, in
order: vault-path selection, a pre-init structure scan of the chosen vault, a
statement of the write contract (writes only under `Knowledge Base/`; existing
files untouched and read-only), Knowledge Base initialization, search-profile
selection (lean/hybrid), a doctor preflight, agent/client registration, skill
installation, optional hook installation, and a per-step summary with next
steps. Each step SHALL report `[done]`, `[skipped: <reason>]`, or
`[failed: <reason>]`.

When the chosen vault already contains non-KB content, setup SHALL offer the
adoption workflow as the next step: scan-only report by default, with explicit
options to save an adoption manifest, copy selected material as sources, or
compile selected material into governed knowledge. Setup SHALL NOT imply that
existing notes must be restructured before Exomem is useful.

#### Scenario: Fresh vault happy path
- **WHEN** `exomem setup` runs against a directory with existing non-KB content
- **THEN** the pre-init scan reports the existing files and the write contract,
  `Knowledge Base/` is created, the skill is installed, and the summary lists
  every step's outcome

#### Scenario: Existing vault routes to adoption
- **WHEN** setup detects substantial existing non-KB content
- **THEN** it states that the content remains untouched/read-only
- **AND** it offers the adoption workflow as a next step instead of telling the
  user to restructure their vault

### Requirement: Setup Teaches The Cognition Layer
The setup summary and first-run docs SHALL explain the simple Exomem model:
built-in AI memory stores preferences/routing; Exomem stores durable governed
knowledge with sources, proof, history, decisions, records, and review. The
summary SHALL include first prompts that use simple verbs rather than internal
ontology.

#### Scenario: First prompts are simple
- **WHEN** setup finishes successfully
- **THEN** the printed next steps include prompts such as "what does this vault
  look like?", "import/adopt my old notes safely", "what do we know about X?",
  "show the sources", and "what needs review?"
- **AND** the prompt examples do not require the user to know internal page
  types
