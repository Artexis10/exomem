## MODIFIED Requirements

### Requirement: Bootstrap serves and teaches the due-state block

The bootstrap payload SHALL carry the due-state block for the requesting audience, subject to the same egress rule as every carrier, and SHALL teach its interpretation in the engagement guidance: counts arrive on the bootstrap payload and never on a write or search reply; a nonzero count is an invitation to consult the review surface, not an instruction to interrupt; before re-raising any surfaced item the agent consults its fingerprint state; moderate signals are agent judgment and silence is preferable to bureaucracy. Post-write guidance SHALL name only fields the default compact response actually carries: it SHALL tell the agent to relay `user_summary`, follow `next` when present and relay `notice` in one sentence, and SHALL name no sync, hash or identity field.

#### Scenario: A fresh session learns the due state at bootstrap

- **WHEN** a session begins with two predictions past their window and one unfinished experiment visible to the audience
- **THEN** the bootstrap payload's due-state block reports those counts with bounded references
- **AND** the engagement guidance in the same payload states how to act on them

#### Scenario: Guidance never names phantom fields

- **WHEN** the bootstrap payload describes post-write feedback
- **THEN** every named response field is present in the default compact response of the writers it describes

#### Scenario: Post-write guidance teaches the minimal reply

- **WHEN** the bootstrap payload describes post-write feedback
- **THEN** it names `user_summary`, `next` and `notice`
- **AND** it names no `*_sync` field, no hash and no operation identity


### Requirement: Bootstrap teaches how to read the capture-sweep advisory

Bootstrap SHALL carry one `capture_sweep_handling` entry in the post-write authoring contract, present on every profile that receives post-write guidance and projected into the skill-aware session view alongside the due-state entries. The entry SHALL be command-free, SHALL state that the pass arrives unasked as the `next` sentence of an ordinary write response, once per episode, SHALL state that `unpaged_mentions` names references without pages, SHALL state that the classes it lists are examples rather than a closed set, SHALL state that the agent decides and the runtime never writes on the pass's behalf, and SHALL state that silence is the right answer when nothing qualifies. Post-write guidance SHALL continue to name only fields the default compact response actually carries.

#### Scenario: A hookless client is taught the block it will receive

- **WHEN** compact bootstrap is rendered for a filtered hosted descriptor
- **THEN** it teaches how to read the capture-sweep `next` sentence without naming a command that descriptor cannot call

#### Scenario: The session projection carries the handling entry

- **WHEN** a client holding the skill contract requests the session bootstrap projection
- **THEN** the projected post-write contract carries the capture-sweep handling entry beside the due-state entries
