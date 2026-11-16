## Purpose

Keep profiles accurate without asking the person to maintain them by hand.

## ADDED Requirements

### Requirement: Curation needs are detected structurally

The background upkeep pass SHALL raise one `upkeep_profile_curation` item for a domain profile when either condition holds:

- live units exist on an evidence page, dated after the profile page's last update, whose categories belong to the registry roles `preferences` or `constraints`;
- a profile line is superseded, is contradicted by a typed relation, has expired, or is past its `check_by` date.

The evidence pages of a domain profile SHALL be the pages in its domain or a descendant domain, together with the pages that the profile page links to.
The evidence pages of a global profile SHALL be the pages that it links to.
Links SHALL be resolved through the product's link resolution and followed one step only.
Detection SHALL resolve roles to categories through the context-role registry. It SHALL use registered domains and typed relations only, SHALL run no model, and SHALL write nothing to the vault.
The product SHALL hold no list of categories, roles or domains in code.

#### Scenario: A new gaming preference raises curation

- **WHEN** a note in the games domain gains a unit of a preference category after the games profile was last updated
- **THEN** the upkeep pass raises one `upkeep_profile_curation` item for the games profile that names the new unit

#### Scenario: A new preference on the linked detail note raises curation

- **WHEN** the games profile links its long detail note, the note is not a member of the games domain, and the note gains a unit of a preference category after the profile was last updated
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

### Requirement: Profile writes need a reviewed preview

A governed write that creates a profile page, replaces one, or changes the content of one, SHALL commit only with the proof returned by a preview of the same resulting content.
The proof of an in-place change is the preview's `transition_token`. The proof of a creation or a replacement is the preview's `draft_id`, `draft_hash` and `draft_token`.
A preview SHALL return that proof beside the resulting lines. The typed tools `create_note`, `edit_note`, `add_observation` and `supersede_note` SHALL take it in an optional `review` field.
A commit with no proof, or with a proof that does not match the content it would write, SHALL be refused and change nothing. The refusal for missing proof SHALL carry the code `PROFILE_PREVIEW_REQUIRED` and name the preview as the remedy.
A removal of a profile line has no preview of its own, so it SHALL be refused with the same code and name the `edit_note` preview as the route.
Writes to other page types SHALL be unaffected.
Editing a profile file directly in the vault SHALL need no preview. The next activation serves the edited lines.

#### Scenario: An unreviewed profile edit is refused

- **WHEN** an agent commits a change to the games profile without first previewing it
- **THEN** the write is refused, the page is unchanged, and the refusal tells the agent to preview the change first

#### Scenario: A reviewed profile edit commits

- **WHEN** an agent previews a change to the games profile, shows the resulting lines, and commits with the returned proof after the owner confirms
- **THEN** the page holds exactly the previewed lines

#### Scenario: A stale review is refused

- **WHEN** an agent previews one change to the games profile and commits different content with that preview's proof
- **THEN** the write is refused and the page is unchanged
