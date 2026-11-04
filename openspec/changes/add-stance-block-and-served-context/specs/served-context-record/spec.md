## Purpose

Keep private, bounded metadata of composed activation responses so an owner can inspect context without claiming client receipt or use.

## ADDED Requirements

### Requirement: One append-only table records the authorized response metadata

Exomem SHALL keep one SQLite table in tenant state beside the heat sidecar. Each successfully recorded maps activation SHALL append one row after all response carriers attach their fields and final egress determines the released material. The row SHALL contain:

- a fresh row id and timestamp;
- authenticated principal and view bindings;
- an existing usable derived session key, or an explicit absent state;
- map generation and whether the map response was unchanged;
- stance state and served line refs when present;
- slice state and served refs with match values;
- the `next_action` state when present;
- surfaced upkeep identities as the final response carries them, including queue item id and fingerprint when supplied.

The row SHALL contain no vault text, conversation field, purpose text or raw caller session. Recording SHALL NOT mint a thread or substitute transport identity. A row proves composed authorized response metadata, not transport receipt, opening, citation or use. Refs identify material without retaining its historical text. Rows SHALL remain unchanged after page rename or deletion; current resolution SHALL be reported separately.

#### Scenario: A cached response gets a fresh row

- **WHEN** two requests use the same cached packet
- **THEN** each successful recording has a distinct id, and the cache contains neither id

#### Scenario: A keyless response remains keyless

- **WHEN** an activation has no usable existing conversation key
- **THEN** its row records that absence and creates no substitute identity

#### Scenario: An unchanged map is distinguishable

- **WHEN** a response returns a map generation with an unchanged reply and no map items
- **THEN** the row records that generation and the unchanged state

#### Scenario: Upkeep matches the final response

- **WHEN** a response surfaces an upkeep item after other carriers run
- **THEN** the row records its final queue identity and supplied fingerprint, without item text

### Requirement: Recording has bounded best-effort latency

Recording SHALL use one bounded SQLite insert with a declared short timeout, no retries and no propagated failure. It SHALL invalidate no heat cache. A successful response SHALL carry `served_context_id` after cache lookup. A failed insert SHALL omit that id and name `served_context_unrecorded` in diagnostics. Its measured stage latency SHALL be reported; it SHALL NOT be described as zero cost.

A successfully recorded raw activation response SHALL carry `served_context_evidence` beside its id, with this fixed structure:

```json
{"scope":"server_composition","client_receipt":"unknown","opening":"unknown","citation":"unknown","use":"unknown","historical_text":"not_retained"}
```

Exact owner lookup SHALL return the same evidence structure and preserve its existing meaning text. The structure SHALL add no counted prose, row, usage inference or read requirement. It SHALL remain outside packet caches. Final serialized sizing SHALL include it. This contract covers raw MCP and REST delivery; it SHALL NOT require hook rendering.

#### Scenario: A composed response states the client observation boundary

- **WHEN** raw activation succeeds and its row is recorded
- **THEN** its response and exact owner lookup carry the fixed composition evidence, without asserting client receipt, opening, citation, use or retained historical text

#### Scenario: A locked database leaves the read served

- **WHEN** the insert cannot complete within its timeout
- **THEN** the read returns its authorized context without an id or composition evidence, with `served_context_unrecorded` in diagnostics

### Requirement: Retention bounds the table

A sweep outside activation SHALL remove rows older than `SERVED_CONTEXT_RETENTION_DAYS` (30) and the oldest rows above `SERVED_CONTEXT_MAX_ROWS` (100,000). Only that sweep SHALL delete rows. Lookup SHALL distinguish absent from expiry supported by retention evidence; missing alone SHALL NOT mean expired.

#### Scenario: A missing id has no inferred history

- **WHEN** lookup finds no row and has no retention evidence for that id
- **THEN** it reports absent, without claiming that the row expired

### Requirement: Only verified owner authority reads rows

Rows SHALL remain in tenant state, including content-private cells, and SHALL never enter host, timing or activation logs. Lookup SHALL verify owner authority before revealing existence or contents. A row id, declared client or session SHALL grant no access. Only verified owner authority SHALL permit reading across principals. The owner command SHALL use one leaf on the CLI and owner REST facade.

#### Scenario: A row id grants no authority

- **WHEN** a nonowner supplies another principal's valid row id
- **THEN** the response discloses neither its existence nor its contents

### Requirement: Summaries describe retained records and unknown usage

The owner command SHALL report retained row count, recorded ref occurrences and first/last retained row timestamps. It SHALL label these as retained records, never all activations. Session sample size SHALL remain unknown wherever usable session identity is absent. Opened and cited observations SHALL be explicitly unknown with reasons that name attribution and coverage limits. The command SHALL NOT join empty session keys, use transport identity as conversation identity or infer causation from timestamp proximity. Missing heat events SHALL NOT establish zero use.

#### Scenario: A transcript read is not attributed telemetry

- **WHEN** a recorded activation is followed by a real read whose heat event has no matching conversation identity
- **THEN** the summary reports opened and cited observations as unknown, even though the read occurred

#### Scenario: Retained timestamps do not prove completeness

- **WHEN** retained rows span thirty days and some insert attempts failed
- **THEN** the summary describes its retained rows and their timestamp range without claiming complete activation coverage

### Requirement: Recording changes no selection and retires no separate ledger

Record operations SHALL change no ranking, selection or authored knowledge. Independent existing heat behavior SHALL remain unchanged. A separate surfaced-item ledger SHALL remain until a replacement proves equivalent identity, fingerprints, surface coverage and durability. This fallible maps record alone SHALL NOT retire it.

#### Scenario: Unopened recorded material keeps its selection

- **WHEN** a ref occurs in many retained rows and the command cannot measure its use
- **THEN** the record changes neither that ref's selection nor its ranking
