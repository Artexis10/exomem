## MODIFIED Requirements

### Requirement: The dispositions view reports each family's effect

The dispositions view SHALL carry an `effect` block computed from existing records only, without an audit. It SHALL state its window as whole UTC days (`days`, with today included, and `since` and `through` as UTC dates), the source of each count, and per family the counts `surfaced`, `dismissed`, `snoozed`, `cleared`, `acted` and `open`. The window SHALL be seven days unless the request sets `window_days`, a whole number from 1 to 90. A value outside that range, or `window_days` on another mode, SHALL be refused with a message that names the allowed range or mode, and SHALL NOT be clamped:

- `surfaced`: identities first stamped on the first-surfaced ledger in the window, and for upkeep families the first deliveries the Dreamer recorded in the window. An identity is one family's item, so an item whose fingerprint changed in the window counts once. It is a lower bound, because an item shown where nothing is recorded is not counted, and the block's `sources` SHALL say so;
- `dismissed` and `snoozed`: items with a manual decision of that action and family updated in the window;
- `cleared`: items surfaced in the window with no decision recorded and absent from the family's current set;
- `acted`: the part of `cleared` that the family can prove was the owner's doing. Only a family that can tell a change from a re-read reports a count; every other family SHALL report `acted` as `unknown`, never 0;
- `open`: items surfaced in the window with no decision recorded and still in the current set.

Events SHALL count from `since` 00:00Z through the time of the read. The stated window SHALL carry no time of day, so the block changes only when a count changes or at a UTC midnight.

The current set SHALL come from the stored due-state projection and the Dreamer's open candidates. A family whose current set only an audit can enumerate SHALL report `cleared` and `open` as `unknown`, never 0. The block SHALL state the Dreamer sidecar's state as `dreamer_sidecar`: `readable`, `missing`, `schema_mismatch`, `unreadable` or `locked`. When it is not `readable`, every upkeep family SHALL be listed, never omitted, with `surfaced`, `cleared` and `open` as `unknown` and an `unknown_reason` that names that state. A cleared item SHALL NOT count as acted without a family that can prove it. The due-state carrier and the attention surface SHALL stamp the family on the ledger; a row that carries no family SHALL be counted under `unattributed` and never assigned a guessed family. The manual dismissal counts SHALL be counted from the decision records without an audit. Both counts and the effect block SHALL be served to the owner only; another bound audience SHALL receive the owner-only aggregate refusal.

#### Scenario: A family's surfacings are accounted for

- **WHEN** a due-state family surfaced three items in the window, one was dismissed, one page was deleted and one is untouched
- **THEN** the family reports `surfaced: 3`, `dismissed: 1`, `cleared: 1` and `open: 1`
- **AND** its `acted` is `unknown` when the family cannot tell a change from a re-read

#### Scenario: The caller chooses the window within one to ninety days

- **WHEN** the dispositions view is requested with `window_days: 30`
- **THEN** the window states 30 days
- **WHEN** it is requested with `window_days: 1`
- **THEN** `since` and `through` are the same UTC date
- **WHEN** it is requested with a value outside 1 to 90, or with `window_days` on another mode
- **THEN** the request is refused with a message that names the range or the mode

#### Scenario: Two reads with no event between them are identical

- **WHEN** the dispositions view is read twice, a second apart in one UTC day, with no event between the reads
- **THEN** both effect blocks are identical

#### Scenario: An audit-only family does not claim zero

- **WHEN** a family surfaced an item but neither the projection nor the Dreamer lists its current set
- **THEN** its `cleared` and `open` are `unknown`

#### Scenario: An unreadable Dreamer sidecar is not read as nothing surfaced

- **WHEN** the Dreamer's sidecar exists but cannot be read
- **THEN** the block reports `dreamer_sidecar: "unreadable"`
- **AND** every upkeep family is listed with `surfaced`, `cleared` and `open` as `unknown` and `unknown_reason: "unreadable"`

#### Scenario: A row stamped before families were is not guessed

- **WHEN** a ledger row carries no family
- **THEN** it is counted under `unattributed`

#### Scenario: The view runs no audit

- **WHEN** the dispositions view is requested
- **THEN** no audit runs, and the manual dismissal counts come from the decision records
