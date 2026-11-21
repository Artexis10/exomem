## ADDED Requirements

### Requirement: A maps packet carries no empty anchors arrays and names its leads

A `maps` packet SHALL omit each of `recent_context`, `anchors`, `roles`, `units`, `pointers`, `current_state`, `missing` and
`ambiguity` whose value is an empty array. A non-empty array SHALL stay. An `anchors` packet SHALL keep its shape.

When a `maps` packet's turn slice is `ready`, no `next_action` is attached and the packet's budget has room for it, the packet
SHALL carry `user_summary`: one sentence that gives the number of leads, names up to three of their titles, and says to read a
ref before relying on it. The sentence SHALL be charged to the budget room that the next action reserves, and a packet
without room for it SHALL carry no `user_summary` and no diagnostic.

#### Scenario: A maps packet leaves out the empty anchors arrays

- **WHEN** activation serves a `maps` packet for a turn
- **THEN** the packet carries none of `recent_context`, `anchors`, `roles`, `units`, `pointers`, `current_state`, `missing` and `ambiguity` that is empty

#### Scenario: The anchors packet keeps its shape

- **WHEN** `EXOMEM_ACTIVATION_PACKET` is `anchors`
- **THEN** the packet carries the anchors arrays as before, empty or not

#### Scenario: A ready slice leads with its leads

- **WHEN** a `maps` packet's turn slice is `ready` with one lead titled "Cargo Sled"
- **THEN** the packet carries `user_summary` "1 lead for this turn: Cargo Sled. Read a ref before relying on it."
- **AND** it carries no `next_action`

#### Scenario: An empty slice keeps its next action

- **WHEN** a `maps` packet's turn slice is `empty`
- **THEN** the packet carries the `slice_empty` `next_action` and no `user_summary`
