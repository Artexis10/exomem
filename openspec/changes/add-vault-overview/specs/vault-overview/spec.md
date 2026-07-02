# vault-overview

## ADDED Requirements

### Requirement: Bounded read-only structure report
The system SHALL provide an `overview` operation that, given a vault subtree
(default: vault root), returns a structural report containing: totals (files,
directories, markdown/binary split, bytes), whether a `Knowledge Base/` tree is
present, a folder tree with per-folder direct/recursive file counts, markdown
frontmatter coverage percentage, wikilink and markdown-link counts, dominant
filename patterns with capped sample names, junk detection (zero-byte files and
sync-conflict-named duplicates), largest and oldest-unmodified file summaries,
and an explicit record of skipped directories and oversized files. The operation
SHALL be read-only and SHALL NOT require optional dependencies.

#### Scenario: Structure report on a messy vault
- **WHEN** `overview` runs on a vault containing nested folders, markdown files
  without frontmatter, binaries, a zero-byte file, and `note 2.md` beside
  `note.md`
- **THEN** the report counts files and folders exactly, reports frontmatter
  coverage below 100%, lists the zero-byte file under junk, lists `note 2.md` as
  a sync-conflict candidate, and marks `kb.present` false when no
  `Knowledge Base/` exists

#### Scenario: Read-only guarantee
- **WHEN** `overview` runs on any vault
- **THEN** no file or directory under the vault is created, modified, or deleted

### Requirement: Output is bounded on arbitrarily large vaults
The report SHALL bound its size by construction: a depth cap (deeper folders
aggregate into their ancestors), a per-level breadth cap with an explicit
`omitted` count, capped list lengths for samples/junk/largest/oldest, and a
per-file content-read cap for markdown stats with over-cap files counted in
`skipped.oversized_files`. Exact totals SHALL be reported alongside every capped
list.

#### Scenario: Caps engage without losing totals
- **WHEN** `overview` runs on a vault whose folder count and junk-file count
  exceed the caps
- **THEN** the tree and junk lists are truncated with explicit omitted/count
  markers while the reported totals remain exact

### Requirement: Reachable from all three doors and callable pre-init
The `overview` operation SHALL be registered as a Tier 1 registry command exposed
via the MCP tool surface, the CLI (`overview` subcommand), and the REST facade
from the same leaf, and SHALL remain exposed when Tier 2 is disabled. The
underlying core function SHALL accept a raw directory path and produce a report
for vaults with no initialized `Knowledge Base/`.

#### Scenario: Tier 2 disabled deployments keep overview
- **WHEN** the server runs with `EXOMEM_DISABLE_TIER2=1`
- **THEN** the `overview` MCP tool is still registered

#### Scenario: Pre-init scan
- **WHEN** the core function runs on a directory that has no `Knowledge Base/`
- **THEN** it returns a full report with `kb.present` false instead of raising

### Requirement: Skill guidance steers agents away from bulk reads
The shipped skill contract SHALL document `overview` as the first step for
assessing vault structure (before `list_directory`, `find scope="vault"`, and
targeted `get`), SHALL include phrasing mappings such as "what does this vault
look like" to the `overview` operation, and SHALL instruct agents never to
bulk-read a vault to answer structural questions. Scaffold wording SHALL remain
generic (no personal or product tokens).

#### Scenario: Scaffold stays generic
- **WHEN** the scaffold leak test runs over the updated SKILL.md
- **THEN** it passes with no personal/brand tokens detected
