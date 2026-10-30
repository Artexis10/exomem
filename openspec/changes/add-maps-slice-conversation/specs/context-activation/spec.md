## ADDED Requirements

### Requirement: The turn slice follows the conversation
In `maps` mode, the turn slice's anchors compile SHALL receive the request's `conversation` and `trigger`, and SHALL apply the conversation-aware activation contract to them as the `anchors` packet does. It SHALL receive no continuity refs, no `anchor` and no attribution, and SHALL NOT mint a token, learn or record heat. A slice lead that the conversation reached SHALL carry the `conversation` evidence kind. The conversation SHALL NOT change the map. For a restricted caller, the compile SHALL read the conversation's refs and entries over the caller's view. The slice's description SHALL say that the block holds what the turn and its conversation reached, with the evidence, and that it is not checked for relevance.

#### Scenario: A follow-up reaches the subject of an earlier turn
- **WHEN** an earlier user entry names the invented anchor "Pelican Ledger", and the follow-up turn reaches "Pelican Ledger" only by one contact kind that alone gives `partial`
- **THEN** the slice lists "Pelican Ledger" as `resolved`, with the `conversation` evidence kind
- **AND** the same turn without `conversation` lists it as `partial`

#### Scenario: An anaphoric follow-up is carried from the conversation
- **WHEN** the newest earlier user entry resolves "Pelican Ledger", and the follow-up turn points back with no words of its own subject
- **THEN** the slice lists "Pelican Ledger" as `partial` with evidence `[conversation]`
- **AND** the same turn without `conversation` gives a slice without "Pelican Ledger"

#### Scenario: A conversation that names a withheld page changes no byte for a restricted caller
- **WHEN** a restricted caller's conversation names an anchor and a page that are withheld from it, in a vault and in its twin that lacks them
- **THEN** the two packets that the caller receives are byte-identical

#### Scenario: The slice compile writes nothing
- **WHEN** a `maps` request carries a `conversation` and a continuity token
- **THEN** activation writes no session, heat or learning record

### Requirement: A maps packet reports its continuity inputs
In `maps` mode, `generation.conversation` SHALL be the state that the turn slice's compile computed for the request's `conversation`. When no slice compile reads the conversation, it SHALL be `absent`. `generation.continuity` SHALL be `absent` when the request carries no token, `applied` when the token carries a map generation, and `stale` for any other token. `generation.continuity_thread` SHALL be `absent`.

#### Scenario: A token reads applied after the map moves
- **WHEN** a caller passes the token of its last `maps` packet after a vault write changed the map generation
- **THEN** `generation.continuity` is `applied`, and the map is sent in full

#### Scenario: A token without a map generation reads stale
- **WHEN** a `maps` request carries a token that cannot be decoded, or an `anchors` token with no map generation
- **THEN** `generation.continuity` is `stale`

#### Scenario: With the slice off, the conversation is not read
- **WHEN** `EXOMEM_ACTIVATION_TURN_SLICE` is `off` and the request carries a `conversation`
- **THEN** `generation.conversation` is `absent`, and the packet is the map alone
