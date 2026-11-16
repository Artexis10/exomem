## Purpose

Keep profiles accurate without asking the person to maintain them by hand.

## ADDED Requirements

### Requirement: Curation needs are detected structurally

The background upkeep pass SHALL raise one `upkeep_profile_curation` item for a domain profile when either condition holds:

- live units exist on pages of the profile's domain or its descendant domains, dated after the profile page's last update, whose categories belong to the registry roles `preferences` or `constraints`;
- a profile line is superseded, is contradicted by a typed relation, has expired, or is past its `check_by` date.

For a global profile, only the second condition SHALL apply.
Detection SHALL resolve roles to categories through the context-role registry. It SHALL use registered domains and typed relations only, SHALL run no model, and SHALL write nothing to the vault.
The product SHALL hold no list of categories, roles or domains in code.

#### Scenario: A new gaming preference raises curation

- **WHEN** a note in the games domain gains a unit of a preference category after the games profile was last updated
- **THEN** the upkeep pass raises one `upkeep_profile_curation` item for the games profile that names the new unit

#### Scenario: A vault's own preference category is detected

- **WHEN** a vault maps its own category, such as `interface_preference`, to the `preferences` role, and a games note gains a unit of that category
- **THEN** the upkeep pass raises the curation item

#### Scenario: A contradicted profile line raises curation

- **WHEN** a later page contradicts a games profile line through a typed relation
- **THEN** the upkeep pass raises one item for the games profile that names the line and the contradicting page

### Requirement: Dismissal holds until the evidence changes

Each item's dismissal fingerprint SHALL be computed over its sorted evidence references.
A dismissed item SHALL stay quiet while its evidence is unchanged.
New evidence SHALL raise the item again.

#### Scenario: A dismissed proposal stays quiet

- **WHEN** the owner dismisses the games curation item, and no new games evidence appears
- **THEN** the item is not raised again; a later new games preference unit raises it again

### Requirement: The connected agent proposes and the owner confirms

An activation response MAY surface a due `upkeep_profile_curation` item to the connected agent through the existing upkeep delivery route, within its existing caps.
The item SHALL name the profile, its evidence references and the governed edit route.
The agent SHALL judge the candidate units with its own model, and SHALL propose the exact lines that would result.
A profile edit SHALL commit only with the owner's confirmation of those exact lines. An explicit owner request for that exact edit supplies it.

#### Scenario: A proposal waits for the owner

- **WHEN** the agent proposes adding a line to the games profile
- **THEN** the page is unchanged until the owner confirms that line
