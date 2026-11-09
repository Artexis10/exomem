## ADDED Requirements

### Requirement: Activation carries the live capture policy
When the request carries no continuity token, or its token names no capture-policy digest or a digest that differs from the current one, `activate_context` SHALL attach one `capture_policy` block. The block SHALL state the effective `proactive_capture` disposition in plain words and, unless that disposition is `off`, the vault's effective `link_acceptance` disposition, the durable-capture classes with their routing and quiet negatives, the check of the relation queue before an affiliation facet, the hand-over rule under `proactive_capture` (keep the exact bytes of every generated file handed to the user), and the route for a reported delivery (search for the artifact's Evidence receipt, then append one delivery Record that links it). The server SHALL build the block from the constants that bootstrap serves, attach it outside the packet cache, reserve its characters inside `limit_chars` from the map's room and not from the turn slice's share, and compute it independently of the turn's content and free of vault content. The server SHALL name the current digest in the continuity token that the response returns when the caller then holds the current policy, because the response carries the block or the request's token already named the digest, and SHALL keep no delivery state of its own. A token that is not valid or a digest field that is absent SHALL read as no digest. The block SHALL NOT exceed 1,200 bytes rendered, header included.

#### Scenario: The block agrees with the bootstrap envelope
- **WHEN** the effective disposition is `off`, `advisory` or `silent`, set by prominence, by an envelope override or by the unreadable-preference floor
- **THEN** the block's `disposition` equals the disposition that bootstrap serves for `proactive_capture` in the same scope

#### Scenario: The block's link disposition equals bootstrap's
- **WHEN** the effective `link_acceptance` is `off`, `advisory` or `confirm-shortcut`, set by derivation from the level or by an envelope override
- **THEN** the block's `link_acceptance` equals the disposition that bootstrap serves for `link_acceptance` in the same scope
- **AND** the block's text states that value, not the derived one

#### Scenario: A link_acceptance change re-arms the block
- **WHEN** a caller holds the block and then the vault's `link_acceptance` changes
- **THEN** the next activation packet carries a block with the new value, even when the request carries the continuity token

#### Scenario: The block is sent once and re-armed
- **WHEN** a caller has received the block and next sends the continuity token that the response returned
- **THEN** the packet carries no block
- **AND** a request without a continuity token carries the block again

#### Scenario: The block is sent once over the stateless transport
- **WHEN** a client calls `recall_context` through the stateless HTTP transport, where every request is a fresh session, and echoes the returned continuity token on each call
- **THEN** only the first packet carries the block

#### Scenario: A policy change reaches the next turn
- **WHEN** a caller saves a new preference after receiving the block
- **THEN** the next activation packet carries a block with the new disposition, even when the request carries the continuity token

#### Scenario: A full map still delivers the block
- **WHEN** the map fills the character budget and the block is owed
- **THEN** the packet carries the block and `used_chars` does not exceed `limit_chars`

#### Scenario: The block does not shrink the turn slice
- **WHEN** a request is owed the block and the turn reaches more leads than fit the slice's share of a budget that has no block
- **THEN** the packet serves the same leads as the same request that holds the policy
- **AND** the map's index is the part that holds fewer items

#### Scenario: A request too small for the block leaves it owed
- **WHEN** a request's budget would leave the map under the smallest budget that a request may ask for
- **THEN** the packet carries no block and the returned token names no digest
- **AND** the next request that echoes that token and has the room carries the block

#### Scenario: A packet with no token carries the block every time
- **WHEN** the packet carries no continuity token, as an abstention under `EXOMEM_DISABLE_WORKING_SET` does
- **THEN** the packet carries the block, and so does every later packet that carries no token

#### Scenario: The quiet disposition carries no doctrine
- **WHEN** the effective disposition is `off`
- **THEN** the block states that writes need an explicit request and carries no capture classes and no `link_acceptance` value
