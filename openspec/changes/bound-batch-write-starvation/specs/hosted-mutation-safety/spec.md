## ADDED Requirements

### Requirement: Foreground writes are not starved by batch or background media work

A keyed write's place in the arrival-order queue SHALL end when its attempt holds the vault mutation boundary, not when its command returns. A multi-unit writer (a multi-path media request or the background media worker) SHALL give way between units to a keyed write that waits in line or at the boundary, for at most a bounded wait per unit, so batch work slows but never stops. Work that publishes a commit's derived registries SHALL NOT keep the boundary held past a short fixed bound: when it cannot finish within the bound, it SHALL complete after the boundary is released, in commit order, closing the commit's publication intents itself, and a cold whole-vault corpus build SHALL NOT run while the boundary is held.

#### Scenario: A keyed edit during a multi-path reprocess commits within about one unit's hold

- **WHEN** a multi-path `process_media` request is committing its paths one at a time
- **AND** a keyed edit arrives while a unit is in flight
- **THEN** the edit acquires the boundary within about one unit's hold plus margin
- **AND** the batch completes every path

#### Scenario: A keyed write is not refused while the boundary sits idle

- **WHEN** a keyed write's command runs longer than a bounded wait while holding no boundary
- **AND** another keyed write arrives behind it
- **THEN** the later write's turn does not wait for the earlier command to return
- **AND** neither write is refused `capture_wait_exhausted` while the boundary is idle

#### Scenario: A blocked publication still ends the boundary hold

- **WHEN** a commit's derived-registry publication cannot finish within the short fixed bound
- **THEN** the boundary releases within that bound
- **AND** the publication completes afterwards, in commit order with the commits around it, closing the commit's publication intents itself
- **AND** the handed-off publication is not republished inside the boundary and mints no full-index receipt
- **AND** no cold whole-vault corpus build runs while the boundary is held
