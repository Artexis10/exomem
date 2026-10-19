## ADDED Requirements

### Requirement: Bootstrap surfaces a recall latency regression to the client experiencing it

The `bootstrap` response SHALL carry a `latency` block only while the latency
watch reports a breach for the calling client, listing per breaching tool the
tool name, the deep flag, the sample count, `p50_ms`, `p90_ms`, `ceiling_ms`
and the dominant spans as name, milliseconds and call count. The block SHALL be
computed from the in-process watch and MUST NOT read the ledger file or any
vault content. A bootstrap response for a client with no breach SHALL be
identical in shape to a response without this capability.

#### Scenario: A healthy service leaves bootstrap unchanged

- **WHEN** the watch reports no breach for the calling client
- **THEN** the bootstrap response has no `latency` key, on every profile

#### Scenario: A breaching client is told what is slow

- **WHEN** the calling client's plain recall p90 is over its ceiling with enough samples
- **THEN** the bootstrap response's `latency` names the tool, `samples`, `p50_ms`, `p90_ms`, `ceiling_ms` and the dominant spans
- **AND** no query text, path or excerpt appears in the block

#### Scenario: Another client's breach is not this client's

- **WHEN** only a different client's recalls are over the ceiling
- **THEN** this client's bootstrap response has no `latency` key
