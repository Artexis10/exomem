## ADDED Requirements

### Requirement: Uncompiled episodes is a backlog family derived from citation

The audit and the due-state projection SHALL carry the family `uncompiled_episode`, with one item per episode whose live recap holds at least one Decided line that no cited revision holds. Revisions of one episode SHALL be the recap Sources that share its frontmatter `episode` key and filename group. A revision SHALL be cited when `source_coverage.evaluate` finds a live compiled page that the caller can see and that names it in `sources:`. A Decided line SHALL be read back with `episode_capture.parse` and SHALL be open when, by exact string equality, no cited revision holds it. A body that parse does not accept SHALL yield no lines.

The item SHALL carry the episode key, the live revision ref, `captured`, `due_since` (equal to `captured`, with no grace period), the Decided count, the open count and a `signal_version` over the live revision, the sorted open lines and the citing pages. A citing page that the caller cannot see SHALL read as absent, so the caller's item equals the item of a vault without that page. The family SHALL describe its result as "cited by a live page (citation only; items unassessed)" and SHALL NOT claim that a page states a decided item. Entity, plan and record pages hold no `sources:` claim, so their updates alone leave an episode uncited. A citation from a compiled note SHALL hold every Decided line in that revision, including entity items in a mixed recap. The agent SHALL compile or explicitly dismiss every Decided item before citing; the server SHALL NOT claim to verify that completion. Recaps SHALL stay out of `unprocessed_source`, so that dismissing a recap never arms the quiet offer on a real Source. The family SHALL sort after `unprocessed_source` in the projection. The due-state projection SHALL serve the family to the owner only, because its stored rows count lines against the owner's view of the citing pages. Another bound audience SHALL receive the family from its own audit, never from the projection.

#### Scenario: A later revision with the same lines is not due

- **WHEN** a live page cites revision 1 of an episode and revision 2 supersedes it with the same Decided lines
- **THEN** the episode has no open line and is not due

#### Scenario: A later revision with a new line is due with that line open

- **WHEN** a live page cites revision 1 and revision 2 adds one Decided line
- **THEN** the episode is due with one open line

#### Scenario: A citing page that the caller cannot see gives the same item as no citation

- **WHEN** the only page that cites a recap is withheld from the caller
- **THEN** the caller's item is the item of a vault where that page does not exist

#### Scenario: A body that is not a recap as recorded holds no lines

- **WHEN** a recap body has been edited by hand so that `episode_capture.parse` rejects it
- **THEN** it yields no Decided lines and closes none

#### Scenario: A write keeps the stored rows as a rebuild would

- **WHEN** a write cites a recap or supersedes one
- **THEN** the stored rows of the whole episode equal the rows of a full rebuild

### Requirement: The uncompiled-episode family ships off and is outside the attention union

The family `uncompiled_episode` SHALL ship with the disposition `off`, declared once with a reason where families are registered, and SHALL NOT add a setting. The owner SHALL turn it on through the existing disposition setting. The dispositions view SHALL list a shipped default as a row until the owner sets a disposition. The family SHALL NOT belong to the default attention categories. It SHALL be optional and absent from the default audit sweep, so that the recaps written before an upgrade do not each surface; an audit that names the family SHALL run it whatever its disposition. While the family is `off`, no due-state block, carrier or default attention view SHALL list it. After the owner sets it to `normal`, an explicit category review SHALL list it. The due-state schema version SHALL change with the new category, so that each install rebuilds its projection once.

#### Scenario: The family ships off, outside attention, and lists once set to normal

- **WHEN** a vault holds an uncited recap and no disposition is set for the family
- **THEN** the dispositions view lists the family as `off`
- **AND** the default attention view and every due-state block omit it
- **WHEN** the owner sets the family to `normal`
- **THEN** an explicit review of the category lists the episode

#### Scenario: A default audit omits the family

- **WHEN** a vault holds an uncited recap and an audit runs without categories
- **THEN** the audit returns no finding of the family
- **WHEN** the audit names the family among its categories
- **THEN** it lists the episode

#### Scenario: A restricted caller is not served the stored rows

- **WHEN** a live page that a restricted audience cannot see cites an earlier revision of an episode, and the owner has set the family to `normal`
- **THEN** the due-state projection serves the family row to the owner and omits it for the restricted audience
- **AND** the restricted audience's audit item equals the item of a vault without the withheld citing page

### Requirement: Episode clearance does not prove an action after surfacing

In the dispositions effect block, the family `uncompiled_episode` SHALL report `acted` as `unknown`. Current citation state SHALL NOT be used to infer an action after surfacing. An item surfaced in the window that left the known current set with no decision recorded SHALL count as `cleared`, including a citation, supersession, deleted recap or dropped line. An item that the owner dismissed or snoozed SHALL count under that decision only. The existing first-surfaced ledger SHALL remain; a fallible response-metadata record SHALL NOT replace it without equivalent identity, fingerprint, surface coverage and durability proof.

#### Scenario: A citation clears an item without proving a later action

- **WHEN** an item was surfaced and a live page then cites its recap
- **THEN** the family reports the item under `cleared` and reports `acted: unknown`

#### Scenario: A citation predating surfacing is not attributed as a later action

- **WHEN** a cached review surfaces an item after its recap was cited, and reconciliation removes the item
- **THEN** the family reports clearance without attributing a new citation after surfacing

#### Scenario: A supersession that drops the open line counts as cleared

- **WHEN** an item was surfaced and a later revision drops the open Decided line
- **THEN** the family reports the item under `cleared` and not under `acted`

#### Scenario: A deleted recap counts as cleared

- **WHEN** an item was surfaced and its recap is deleted
- **THEN** the family reports the item under `cleared` and not under `acted`
