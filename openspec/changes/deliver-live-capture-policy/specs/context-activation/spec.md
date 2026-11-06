## ADDED Requirements

### Requirement: Activation carries the live capture policy
When the request carries no continuity token, or the capture-policy digest differs from the one last delivered to the caller's session, `activate_context` SHALL attach one `capture_policy` block. The block SHALL state the effective `proactive_capture` disposition in plain words and, unless that disposition is `off`, the durable-capture classes with their routing and quiet negatives. The server SHALL build the block from the constants that bootstrap serves, attach it outside the packet cache, reserve its characters inside `limit_chars`, and compute it independently of the turn's content and free of vault content. The server SHALL record delivery on the due-state emission ledger only when the response carries the block. The block SHALL NOT exceed 800 bytes.

#### Scenario: The block agrees with the bootstrap envelope
- **WHEN** the effective disposition is `off`, `advisory` or `silent`, set by prominence, by an envelope override or by the unreadable-preference floor
- **THEN** the block's `disposition` equals the disposition that bootstrap serves for `proactive_capture` in the same scope

#### Scenario: The block is sent once and re-armed
- **WHEN** a caller has received the block and next sends the continuity token
- **THEN** the packet carries no block
- **AND** a request without a continuity token carries the block again

#### Scenario: A policy change reaches the next turn
- **WHEN** a caller saves a new preference after receiving the block
- **THEN** the next activation packet carries a block with the new disposition, even when the request carries the continuity token

#### Scenario: A full map still delivers the block
- **WHEN** the map fills the character budget and the block is owed
- **THEN** the packet carries the block and `used_chars` does not exceed `limit_chars`

#### Scenario: The quiet disposition carries no doctrine
- **WHEN** the effective disposition is `off`
- **THEN** the block states that writes need an explicit request and carries no capture classes
