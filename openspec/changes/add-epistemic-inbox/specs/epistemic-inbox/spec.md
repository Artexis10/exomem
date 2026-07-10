## ADDED Requirements

### Requirement: Stable addressable review items
Every attention item SHALL carry a deterministic item ID, an `exomem://review/<id>` reference, a canonical target reference, canonical related references, and a signal fingerprint. Item identity SHALL be independent of rank, age counters, and rendered detail text.

#### Scenario: Ranking changes do not change identity
- **WHEN** the same target appears at a different attention rank on a later run with the same signal
- **THEN** its item ID, review reference, and fingerprint are unchanged

#### Scenario: Material signal change updates fingerprint
- **WHEN** the target content, contributing categories, or contradiction partner changes
- **THEN** the item keeps its stable review reference and receives a different fingerprint

### Requirement: Portable fingerprint-bound review state
The system SHALL persist dismiss and snooze decisions in versioned JSON at `Knowledge Base/.review-state.json`, keyed by item ID and reviewed fingerprint. Default review SHALL omit matching dismissed items and unexpired matching snoozes. Expired snoozes and fingerprint-mismatched records SHALL resurface automatically.

#### Scenario: Dismissed signal stays out of the open inbox
- **WHEN** a user dismisses a review item and the underlying fingerprint remains unchanged
- **THEN** default review omits it and an all/dismissed state query can still inspect it

#### Scenario: Changed dismissed signal resurfaces
- **WHEN** a dismissed item's underlying note or related signal changes enough to produce a new fingerprint
- **THEN** default review surfaces the item as open again without deleting its prior state record

#### Scenario: Snooze expires
- **WHEN** an item's snooze-until date has passed
- **THEN** the item returns to the open inbox deterministically

### Requirement: Explicit triage command
The system SHALL expose a write-capable `triage_memory` command with `dismiss`, `snooze`, and `reopen` actions over MCP, REST, and CLI from one registry definition. It SHALL resolve `exomem://review/<id>` references, validate snooze dates, atomically update review state, and return the resulting item state. `review_memory` SHALL remain read-only.

#### Scenario: Triage permissions remain separate from review
- **WHEN** command metadata is inspected
- **THEN** `review_memory` is read-only and `triage_memory` is write-capable

#### Scenario: Reopen clears active triage state
- **WHEN** `triage_memory(action="reopen")` receives a known review reference
- **THEN** the matching dismiss/snooze state is cleared and the item can appear in the open inbox

### Requirement: Human daily-review command
`exomem review` SHALL render a compact human-readable inbox by default with item number, severity/category, target, reason, stable review reference, and counts. `--json` SHALL retain the shared machine envelope. The alias SHALL expose dismiss, snooze, and reopen subcommands that route to `triage_memory`.

#### Scenario: Human and automation output remain distinct
- **WHEN** a user runs `exomem review`
- **THEN** output is a concise review list rather than a raw JSON document
- **AND** `exomem review --json` returns the registry-derived JSON envelope

### Requirement: State failure is explicit and resource cost is negligible
Absent review state SHALL behave as an empty state without creating a file. Malformed or unsupported state SHALL return an explicit error and SHALL NOT be overwritten. Review identity, filtering, and triage SHALL require no model, background process, or resident resource.

#### Scenario: First read is side-effect free
- **WHEN** review runs in a vault without `.review-state.json`
- **THEN** all current items are open and no state file is created
