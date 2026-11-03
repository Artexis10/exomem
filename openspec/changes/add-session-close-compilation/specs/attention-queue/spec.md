## ADDED Requirements

### Requirement: Uncompiled episodes is a backlog family derived from citation

The audit and the due-state projection SHALL carry the family `uncompiled_episode`, with one item per episode whose live recap holds at least one Decided line that no cited revision holds. Revisions of one episode SHALL be the recap Sources that share its frontmatter `episode` key and filename group. A revision SHALL be cited when `source_coverage.evaluate` finds a live compiled page that the caller can see and that names it in `sources:`. A Decided line SHALL be read back with `episode_capture.parse` and SHALL be open when, by exact string equality, no cited revision holds it. A body that parse does not accept SHALL yield no lines.

The item SHALL carry the episode key, the live revision ref, `captured`, `due_since` (equal to `captured`, with no grace period), the Decided count, the open count and a `signal_version` over the live revision, the sorted open lines and the citing pages. A citing page that the caller cannot see SHALL read as absent, so the caller's item equals the item of a vault without that page. The family SHALL describe its result as "cited by a live page (citation only; items unassessed)" and SHALL NOT claim that a page states a decided item. Entity, plan and record pages hold no `sources:` claim, so a compile into one SHALL leave its line open. Recaps SHALL stay out of `unprocessed_source`, so that dismissing a recap never arms the quiet offer on a real Source. The family SHALL sort after `unprocessed_source` in the projection.

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

The family `uncompiled_episode` SHALL ship with the disposition `off`, declared once with a reason where families are registered, and SHALL NOT add a setting. The owner SHALL turn it on through the existing disposition setting. The dispositions view SHALL list a shipped default as a row until the owner sets a disposition. The family SHALL NOT belong to the default attention categories. While the family is `off`, no due-state block, carrier or default attention view SHALL list it. After the owner sets it to `normal`, an explicit category review SHALL list it. The due-state schema version SHALL change with the new category, so that each install rebuilds its projection once.

#### Scenario: The family ships off, outside attention, and lists once set to normal

- **WHEN** a vault holds an uncited recap and no disposition is set for the family
- **THEN** the dispositions view lists the family as `off`
- **AND** the default attention view and every due-state block omit it
- **WHEN** the owner sets the family to `normal`
- **THEN** an explicit review of the category lists the episode

### Requirement: An uncompiled episode counts as acted only when a live page cites it

In the dispositions effect block, the family `uncompiled_episode` SHALL report `acted` as the count of items that were surfaced in the window and then left the open set because their recap is still live and every Decided line is now held by a cited revision. An item that left the open set through supersession, deletion of the recap or a dropped line SHALL count as `cleared` and SHALL NOT count as `acted`. An item that the owner dismissed or snoozed SHALL count under that decision only.

#### Scenario: A citation counts as acted

- **WHEN** an item was surfaced and a live page then cites its recap
- **THEN** the family reports the item under `acted`

#### Scenario: A supersession that drops the open line counts as cleared

- **WHEN** an item was surfaced and a later revision drops the open Decided line
- **THEN** the family reports the item under `cleared` and not under `acted`

#### Scenario: A deleted recap counts as cleared

- **WHEN** an item was surfaced and its recap is deleted
- **THEN** the family reports the item under `cleared` and not under `acted`
