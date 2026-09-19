# agent-bootstrap-contract (delta)

## ADDED Requirements

### Requirement: Generic guidance carries the activation line
At balanced and maximal prominence, the generic client workflow guidance served by
bootstrap SHALL include one line instructing the agent to call `activate_context`
with the user's turn before answering a substantive turn that has no prior
conversation context, and to resolve a returned `ambiguous` packet by calling again
with `anchor` set; at light and off it SHALL be absent. The line SHALL cost at most 220 bytes in the
served JSON and SHALL be byte-identical between the served projection and the shipped
scaffold's recall loop. With the line present the compact profile SHALL stay within its
byte ceiling at every prominence level and on every client surface, and SHALL keep at
least its 512-byte headroom warning margin at the default (balanced) level. The maximal
level was already inside that warning margin before this line existed, so the margin is
not required of it; its measured headroom SHALL be recorded beside the ceiling constant.

#### Scenario: Ceiling holds at every level, margin at the default
- **WHEN** the compact bootstrap is served at each of off, light, balanced and maximal,
  for the generic surface and for a hook-capable client surface
- **THEN** every projection is within the compact byte ceiling, and the balanced
  projections keep at least 512 bytes of headroom

#### Scenario: Line present at maximal, absent at light
- **WHEN** bootstrap is served for an identity whose effective level is maximal and
  again for one whose level is light
- **THEN** the first guidance contains the activation line and the second does not,
  and the compact profile stays within its ceiling in both cases
