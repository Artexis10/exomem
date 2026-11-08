## ADDED Requirements

### Requirement: Setup offers the learning instruction
`exomem setup` SHALL offer to place Exomem's learning instruction in the user-level instruction file of each client that it wires: `CLAUDE.md` in the Claude Code configuration root and `AGENTS.md` in the Codex home. Setup SHALL show the text and ask before it writes. It SHALL write the text between named begin and end markers and change nothing outside them. A non-interactive run SHALL write the text only when an explicit flag asks for it. Setup SHALL NOT offer the step while the effective prominence is `off` or `light`. Uninstall SHALL remove only the marked span.

#### Scenario: Interactive setup places the instruction after consent
- **WHEN** a user runs `exomem setup` interactively with Claude Code detected and the effective prominence is `balanced`
- **THEN** setup shows the learning instruction and asks whether to add it to the Claude Code `CLAUDE.md`
- **AND** on a yes, the file holds the text between the markers, and every line outside the markers is unchanged

#### Scenario: A re-run updates the marked span only
- **WHEN** setup runs again after the packaged text changed
- **THEN** setup replaces the text between the markers and reports the update
- **AND** text the user wrote outside the markers stays byte for byte

#### Scenario: A scripted setup does not edit the file unasked
- **WHEN** a user runs `exomem setup --yes` without the learning-instruction flag
- **THEN** setup does not create or change any client instruction file
- **AND** the summary names the flag that would add the instruction

#### Scenario: Quiet prominence is respected
- **WHEN** the effective prominence is `off` or `light`
- **THEN** setup does not offer the learning instruction

#### Scenario: Uninstall removes only the marked span
- **WHEN** the user removes Exomem's client integration
- **THEN** the marked span is gone and every other line of the file is unchanged
