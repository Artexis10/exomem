# semantic-unit-retrieval Specification

## Purpose
TBD - created by archiving change add-epistemic-loop-primitives. Update Purpose after archive.
## Requirements
### Requirement: Unit Hits Carry Governed Metadata
A semantic-unit hit SHALL expose the unit's governed `verdict` and `check_by` metadata when present and SHALL omit those keys when absent, so a caller can tell a judged unit from an unjudged one without re-reading the parent page. The governed egress projector SHALL register both fields, and the compact unit projection SHALL carry `verdict` when present so the default result shape distinguishes a refuted unit at a glance.

#### Scenario: A judged unit hit exposes its verdict
- **WHEN** a semantic-unit hit is serialized for a unit carrying `- verdict: refuted` and `- check_by: 2026-11-01`
- **THEN** the serialized hit contains that verdict and that check-by date

#### Scenario: An unjudged unit hit omits the keys
- **WHEN** a semantic-unit hit is serialized for a unit carrying neither governed metadata row
- **THEN** the serialized hit contains neither key

#### Scenario: The compact projection keeps the verdict
- **WHEN** the compact projection of a unit hit carrying `- verdict: refuted` is serialized
- **THEN** the compact payload contains that verdict

#### Scenario: Governed egress recognizes the fields
- **WHEN** the registered semantic-unit projector field set is inspected
- **THEN** it contains both governed metadata field names

### Requirement: Verdict Is State, Not Supersession Or Rank
A `verdict` SHALL NOT change retrieval ranking, SHALL NOT mark a unit or its parent superseded, and SHALL NOT exempt a unit from page-status inheritance. A refuted unit on an active page SHALL remain retrievable at the same standing as any other active unit, and SHALL remain distinguishable from both an unjudged active unit and a unit whose parent page is superseded.

#### Scenario: A refuted unit ranks identically to an unjudged one
- **WHEN** two otherwise identical units differ only in that one carries `- verdict: refuted`
- **THEN** the serialized ranking signals of their hits are equal

#### Scenario: A refuted unit does not become superseded
- **WHEN** a unit on an active page carries `- verdict: refuted`
- **THEN** its hit reports the parent status as active and reports no superseding page

#### Scenario: Page status still governs a judged unit
- **WHEN** a unit carrying `- verdict: confirmed` sits on a superseded page
- **THEN** its hit reports the parent status as superseded exactly as an unjudged unit would
