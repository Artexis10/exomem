## ADDED Requirements

### Requirement: Skill discovers vault layout at runtime

The skill SHALL instruct the agent to learn a vault's structure by calling the
`overview` tool on first engagement, rather than assuming a fixed folder layout, and
SHALL treat every path outside `Knowledge Base/` as read-only input.

#### Scenario: First engagement in an unfamiliar vault
- **WHEN** the agent begins working in a vault it has not seen this session
- **THEN** the skill directs it to run `overview` once to learn the actual top-level
  layout, and to treat all folders outside `Knowledge Base/` as read-only input
  (linkable, never written)

#### Scenario: No assumed vault shape
- **WHEN** a user's vault contains top-level folders other than the illustrative ones
- **THEN** the skill does not require or assume any specific sibling-folder names;
  only `Knowledge Base/` is governed and writeable

### Requirement: Personalize classifies sibling folders by measured signals

`exomem personalize` SHALL scan the vault via `overview` and classify each top-level
folder outside `Knowledge Base/` as `readonly`, `excluded`, or unmanaged using measured
signals only (file counts, markdown/binary ratio, junk ratio) — never folder-name
denylists.

#### Scenario: Markdown sibling defaults to readonly
- **WHEN** a sibling folder contains markdown files
- **THEN** it is classified `readonly` (findable, write-protected)

#### Scenario: Binary-heavy folder is excluded
- **WHEN** a sibling folder has no markdown and is ≥90% binary files
- **THEN** it is classified `excluded`

#### Scenario: Junk-dominant folder is excluded
- **WHEN** ≥50% of a sibling folder's files are sync-conflict or zero-byte
- **THEN** it is classified `excluded`

#### Scenario: Empty folder is left unmanaged
- **WHEN** a sibling folder contains no files
- **THEN** it is left unmanaged (no `_access.yaml` entry is written)

### Requirement: Personalize writes a non-destructive access policy

Personalize SHALL write and merge `<vault>/Knowledge Base/_access.yaml`
non-destructively: preserving existing entries and unknown keys, never re-proposing
already-configured folders, and producing byte-stable idempotent output. The emitted
file SHALL be honored by the access layer.

#### Scenario: Fresh policy is honored by the access layer
- **WHEN** personalize writes `_access.yaml` for a vault with a markdown `Reference/`
  and a binary `Photos/`
- **THEN** the access layer resolves `Reference/` as read-only and `Photos/` as excluded

#### Scenario: Re-run makes no changes
- **WHEN** personalize runs again on an already-governed vault
- **THEN** it reports no changes and the file bytes are unchanged

#### Scenario: Existing user entries preserved
- **WHEN** the user has hand-added entries to `_access.yaml`
- **THEN** personalize preserves them and only appends newly proposed entries

### Requirement: Personalize soft-fails and is human-gated

Personalize SHALL require an initialized `Knowledge Base/` and otherwise fail with a
structured error without writing; SHALL run no model and make no network call; and SHALL
write only after confirmation (or `--yes`). It SHALL NOT be exposed as an MCP write tool
in this version.

#### Scenario: Missing Knowledge Base
- **WHEN** personalize runs against a vault with no `Knowledge Base/`
- **THEN** it raises a structured error and writes nothing

#### Scenario: Interactive decline
- **WHEN** the user declines the proposal at the prompt
- **THEN** nothing is written and the command exits successfully

#### Scenario: Non-interactive apply
- **WHEN** personalize runs with `--yes`
- **THEN** it applies the proposed defaults without prompting

### Requirement: Access overrides are documented and the skill is markdown-first

The skill contract SHALL document `Knowledge Base/_access.yaml` (`readonly`/`excluded`
subtree prefixes, hot-reloaded, hard write-refusal with no override) and SHALL describe
the store as a markdown vault (Obsidian optional), keeping wikilinks and YAML frontmatter
stated as required while marking Dataview, callouts, and file-sync as optional.

#### Scenario: Access config is user-visible
- **WHEN** a user reads the skill's write-scope reference
- **THEN** it documents the `_access.yaml` `readonly`/`excluded` schema and semantics

#### Scenario: Markdown-first framing
- **WHEN** a user reads the skill
- **THEN** it refers to a "markdown vault (Obsidian optional)" and marks Dataview /
  callouts / Obsidian Sync as optional niceties, not requirements
