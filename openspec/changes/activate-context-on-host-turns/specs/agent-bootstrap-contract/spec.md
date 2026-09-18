# agent-bootstrap-contract (delta)

## ADDED Requirements

### Requirement: Generic guidance carries the activation line
At balanced and maximal prominence, the generic client workflow guidance served by
bootstrap SHALL include one line instructing the agent to call `activate_context`
with the user's turn before answering a substantive turn that has no prior
conversation context, and to resolve a returned `ambiguous` packet by calling again
with `anchor` set; at light and off it SHALL be absent. The line SHALL fit within
the compact profile's byte ceiling and SHALL be byte-identical between the served
projection and the shipped scaffold's recall loop.

#### Scenario: Line present at maximal, absent at light
- **WHEN** bootstrap is served for an identity whose effective level is maximal and
  again for one whose level is light
- **THEN** the first guidance contains the activation line and the second does not,
  and the compact profile stays within its ceiling in both cases
