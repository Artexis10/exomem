## ADDED Requirements

### Requirement: A served maps packet carries no empty anchors arrays

A `maps` packet that carries a `map` SHALL omit each of `recent_context`, `anchors`, `roles`, `units`, `pointers`,
`current_state`, `missing` and `ambiguity` whose value is an empty array. A non-empty array SHALL stay. An `anchors` packet
and an abstained packet that carries no `map` SHALL keep their shape.

#### Scenario: A maps packet leaves out the empty anchors arrays

- **WHEN** activation serves a `maps` packet for a turn
- **THEN** the packet carries none of `recent_context`, `anchors`, `roles`, `units`, `pointers`, `current_state`, `missing` and `ambiguity` that is empty

#### Scenario: The anchors packet keeps its shape

- **WHEN** `EXOMEM_ACTIVATION_PACKET` is `anchors`
- **THEN** the packet carries the anchors arrays as before, empty or not
