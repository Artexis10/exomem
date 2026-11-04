## MODIFIED Requirements

### Requirement: Generic guidance carries the activation line
At balanced and maximal prominence, bootstrap's generic workflow guidance SHALL
include its capability-selected recall carrier before a substantive turn with no
prior context. Use the existing activation carrier where activation is exposed,
and the existing search carrier on reduced surfaces that expose search without
activation. At light and off this initiated-recall line SHALL be absent.

Each carrier SHALL add the command-independent sentence "Follow `next_action` when
present." and SHALL name no unavailable call. Per-state moves SHALL come from the
shared source. Each served carrier SHALL cost at most 220 bytes and SHALL match
its corresponding scaffold projection byte for byte; different capability
projections need not contain identical call text. The compact payload SHALL stay
within its ceiling on every surface and prominence level. Balanced SHALL retain
at least 512 bytes of headroom. Maximal's measured headroom SHALL remain recorded
beside the ceiling constant, without a new 512-byte requirement.

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

#### Scenario: The line points at the next move
- **WHEN** bootstrap is served at balanced
- **THEN** the selected recall carrier tells the agent to follow `next_action` when present,
  and it names no per-state move or unavailable call

## ADDED Requirements

### Requirement: Bootstrap teaches the authorized stance-line route
At balanced and maximal prominence, the client model MAY initiate a proposal for a correction that applies to every task. It SHALL propose the exact line and cite the user's words. It SHALL write only with authority for that exact global line and any page promotion. An explicit request for that exact action SHALL count as authority without another confirmation. Otherwise an initiated proposal SHALL wait for confirmation. Marking an existing page SHALL cover every resulting global unit.

At light, write only on request. At off, initiate no capture or proposal, but honor an explicit request. These rules SHALL preserve the existing engagement gate. Scoped rules SHALL remain ordinary compiled knowledge. Replacements SHALL use same-page unit supersession or existing page supersession. No server cue list or classifier SHALL judge correction meaning.

The scaffold, its deployed skill copies, prominence and bootstrap SHALL derive this rule from their maintained source path. Preserve bootstrap's capability-aware activation or search carrier; do not replace it with an unconditional literal tool call. The next-action sentence SHALL be command-independent, and the compact payload SHALL retain its existing ceiling and default margin.

#### Scenario: An unconfirmed proposal is not written
- **WHEN** the agent initiates a proposal and the user declines or gives no confirmation
- **THEN** no stance line or page promotion is written

#### Scenario: An exact request at off remains actionable
- **WHEN** at off the user explicitly asks to save an exact global stance line with its source
- **THEN** the agent makes that authorized write without a second confirmation

#### Scenario: A scoped correction stays out of stance
- **WHEN** a correction applies to one entity or one kind of task
- **THEN** the agent does not promote it into the global stance block
