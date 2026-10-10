# existing-vault-onboarding

## ADDED Requirements

### Requirement: Existing-vault onboarding is documented
The setup documentation SHALL contain a section for users whose vault already
holds content, stating: that exomem writes only under `Knowledge Base/` and
never modifies, moves, or restructures existing files; that existing notes
remain searchable (`find` scope widening) and assessable (`overview`); guidance
on initializing into the same vault (default) versus a separate vault
(isolation exception); and guidance specific to daily-notes vaults, including
that no frontmatter, links, or restructuring are ever required of existing
notes.

#### Scenario: A daily-notes user reads the section
- **WHEN** a user with a vault of dated daily logs reads the existing-vault
  section before running setup
- **THEN** the section tells them their log stays untouched, the KB lands
  beside it, and the first thing to ask Claude is "what does this vault look
  like" (answered by `overview` in one call, not per-file reads)

#### Scenario: README routes to the canonical section
- **WHEN** a user reads the README quickstart with an existing vault in mind
- **THEN** the quickstart names the existing-vault case and links to the
  canonical section in the setup doc
