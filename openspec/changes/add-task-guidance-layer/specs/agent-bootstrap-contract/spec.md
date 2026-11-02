## MODIFIED Requirements

### Requirement: Generic guidance carries the activation line
At balanced and maximal prominence, the generic client workflow guidance served by
bootstrap SHALL include one line instructing the agent to call `activate_context`
with the user's turn before answering a substantive turn that has no prior
conversation context, and to follow a returned `next_action` when present
(read-next-action); at light and off it SHALL be absent. The line SHALL NOT repeat a
per-state move, such as the `anchor` call for an `ambiguous` packet, because the
`next_action` state table holds it. The line SHALL cost at most 220 bytes in the
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

#### Scenario: The line points at the next move
- **WHEN** bootstrap is served at balanced
- **THEN** the activation line tells the agent to follow `next_action` when present,
  and it names no per-state move

## ADDED Requirements

### Requirement: Bootstrap teaches the correction route
At `balanced` and `maximal`, the capture axis of the engagement contract SHALL name a
behavioural correction as a capture class beside its existing classes. The
client model SHALL judge whether a user statement corrects how the agent should
behave, and which kind it is: a stance line that applies to every task, or a
precedent or failure unit that applies to one activity. The agent SHALL propose the
exact unit and cite the user's words as its source, either an episode `said` line or
a captured Source, and SHALL write it only after the user confirms in the
conversation, recording `guidance_origin: reviewed` (task-guidance). After the write,
the agent SHALL call `recall_context` with `trigger = "task_refresh"` and
`refresh_reason = "correction"`. A later correction SHALL replace an earlier one
through `supersedes`, and a conflict SHALL be marked with `contradicts`. At `light`
the agent SHALL write a correction only when asked, and at `off` it SHALL only
propose. The server SHALL add no correction detector: no word list, cue list or
classifier SHALL decide that a statement is a correction. Both `SKILL.md` copies,
`prominence.py`, the scaffold's recall loop and the bootstrap `intent_boundary` and
`capture_examples` SHALL carry the same rule, and the compact payload SHALL stay within
its measured ceiling.

#### Scenario: A confirmed correction becomes a stance line
- **WHEN** at `balanced` the user says that the agent must never treat a draft as final
  without a stated review, and confirms the agent's proposed stance line
- **THEN** the agent writes the line on a stance page with `guidance_origin: reviewed`
  and its source, refreshes with `refresh_reason = "correction"`, and the next packet's
  stance block serves the line

#### Scenario: An unconfirmed proposal is not served
- **WHEN** the agent proposes a precedent unit for `reply-message` and the user does not
  confirm it
- **THEN** no reviewed declaration is written, and no packet serves the proposal

#### Scenario: Off only proposes
- **WHEN** the engagement level is `off` and the user corrects the agent's behaviour
- **THEN** the agent may propose the exact unit and its source, and writes nothing
