## ADDED Requirements

### Requirement: The upkeep review states why it is unavailable

When `review_memory(mode="upkeep")` answers `status: "unavailable"`, the response SHALL carry a `reason` drawn from exactly `worker_not_running`, `no_tick_yet`, `schema_mismatch` and `unreadable`. A `no_tick_yet` response SHALL carry a `waiting` object with the gate's reason code, the UTC time it began, and a `source` of `worker` (the serving process's live state) or `sidecar` (the state the worker last recorded). A `sidecar` wait SHALL also carry `recorded_at`, the UTC time the worker wrote it, so a stopped worker's last record never reads as a live wait. The reason SHALL come from the worker's live state in the process that hosts the worker, and otherwise from what the worker recorded in its own sidecar, never from the empty state of a process that runs no worker. The worker SHALL record its health at the end of a tick and, within one poll, whenever its gate holds it for a reason other than the one on record, writing at most once per poll, so a process without the worker can read why no tick has run. A sidecar that no tick has written SHALL report `no_tick_yet`, not `available`. Off, paused and standby workers SHALL create no sidecar for this record.

#### Scenario: A worker held by its gate says why to another process

- **WHEN** the worker is running and its gate holds it before any tick, and a process without the worker reviews upkeep
- **THEN** the response is `unavailable` with `reason: "no_tick_yet"`
- **AND** `waiting` names the gate's reason, when it began, `source: "sidecar"`, and when the worker recorded it

#### Scenario: No worker has run against this state root

- **WHEN** no sidecar exists and no worker runs in the reviewing process
- **THEN** the response is `unavailable` with `reason: "worker_not_running"`

#### Scenario: A sidecar the reviewer cannot read names itself

- **WHEN** the sidecar exists with an older schema, or cannot be read
- **THEN** the reason is `schema_mismatch` or `unreadable` respectively

### Requirement: The dispositions view reports each family's effect

The dispositions view SHALL carry an `effect` block computed from existing records only, without an audit. It SHALL state its window (`since`, `until` and `days`, fixed at seven days), the source of each count, and per family the counts `surfaced`, `dismissed`, `snoozed`, `cleared` and `open`:

- `surfaced`: identities first stamped on the first-surfaced ledger in the window, and for upkeep families the first deliveries the Dreamer recorded in the window. An identity is one family's item, so an item whose fingerprint changed in the window counts once;
- `dismissed` and `snoozed`: items with a manual decision of that action and family updated in the window;
- `cleared`: items surfaced in the window with no decision recorded and absent from the family's current set;
- `open`: items surfaced in the window with no decision recorded and still in the current set.

The current set SHALL come from the stored due-state projection and the Dreamer's open candidates. A family whose current set only an audit can enumerate SHALL report `cleared` and `open` as `unknown`, never 0. The block SHALL state the Dreamer sidecar's state as `dreamer_sidecar`: `readable`, `missing`, `schema_mismatch`, `unreadable` or `locked`. When it is not `readable`, every upkeep family SHALL be listed, never omitted, with `surfaced`, `cleared` and `open` as `unknown` and an `unknown_reason` that names that state. The count SHALL be named `cleared`, never `acted`. The due-state carrier and the attention surface SHALL stamp the family on the ledger; a row that carries no family SHALL be counted under `unattributed` and never assigned a guessed family. The manual dismissal counts SHALL be counted from the decision records without an audit. Both counts and the effect block SHALL be served to the owner only; another bound audience SHALL receive the owner-only aggregate refusal.

#### Scenario: A family's surfacings are accounted for

- **WHEN** a due-state family surfaced three items in the window, one was dismissed, one page was deleted and one is untouched
- **THEN** the family reports `surfaced: 3`, `dismissed: 1`, `cleared: 1` and `open: 1`

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
