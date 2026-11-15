## Purpose

Keep profiles accurate without asking the person to maintain them by hand.

## ADDED Requirements

### Requirement: Curation needs are detected structurally

The background upkeep pass SHALL raise a `profile_curation` upkeep item for a profile when any of these holds:
- new durable units of the preference or constraint roles exist in the profile's domain since its last curation;
- a profile line is superseded, contradicted by a typed relation, expired or past its review date.
Detection SHALL use registered roles, domains and typed relations only, and SHALL run no model.

#### Scenario: A new gaming preference raises curation

- **WHEN** a note gains a preference unit in the gaming domain after the gaming profile's last curation
- **THEN** the upkeep pass raises one `profile_curation` item for the gaming profile that names the new unit

### Requirement: The connected agent proposes and the owner confirms

An activation response MAY surface a due `profile_curation` item to the connected agent through the existing upkeep route.
The agent SHALL judge the candidate units with its own model and propose profile edits as a governed preview that names every resulting line.
A profile edit SHALL commit only with the owner's confirmation of those exact lines; an explicit owner request for that exact edit supplies it.
Dismissing the item SHALL record the decision so the same candidates are not raised again.

#### Scenario: A proposal waits for the owner

- **WHEN** the agent proposes adding a line to the gaming profile
- **THEN** the page is unchanged until the owner confirms that line
