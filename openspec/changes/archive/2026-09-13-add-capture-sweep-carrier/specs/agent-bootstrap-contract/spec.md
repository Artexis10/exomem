## ADDED Requirements

### Requirement: Bootstrap teaches how to read the capture-sweep advisory

Bootstrap SHALL carry one `capture_sweep_handling` entry in the post-write authoring contract, present on every profile that receives post-write guidance and projected into the skill-aware session view alongside the due-state entries. The entry SHALL be command-free, SHALL state that the block arrives unasked on an ordinary write response, SHALL state that the classes it lists are examples rather than a closed set, SHALL state that the agent decides and the runtime never writes on the block's behalf, and SHALL state that silence is the right answer when nothing qualifies. Post-write guidance SHALL continue to name only fields the default compact response actually carries.

#### Scenario: A hookless client is taught the block it will receive

- **WHEN** compact bootstrap is rendered for a filtered hosted descriptor
- **THEN** it teaches how to read `capture_sweep` without naming a command that descriptor cannot call

#### Scenario: The session projection carries the handling entry

- **WHEN** a client holding the skill contract requests the session bootstrap projection
- **THEN** the projected post-write contract carries the capture-sweep handling entry beside the due-state entries

### Requirement: The capture predicate names a bounded episode-completeness pass

The `balanced` and `maximal` prominence capture contracts SHALL each carry one clause asking for a single bounded pass over the recent exchange after any capture, over anything else that passes the same reuse-value test. The clause SHALL present its classes as examples and never as a closed enumeration, SHALL say that what the response lists as recently written is not re-written, and SHALL say that silence is correct when nothing qualifies. The `light` and `off` contracts SHALL be unchanged and SHALL NOT become proactive.

Each carrier's capture text SHALL remain a superset of the text it held before this change, in every projection that serves it. The existing compact bootstrap byte ceiling SHALL NOT be raised, and the compact payload size at `balanced` and `maximal` SHALL be recorded before and after.

#### Scenario: Prominent levels carry the pass

- **WHEN** the prominence capture contract is read at `balanced` or `maximal`
- **THEN** it names the bounded pass, marks its classes as examples, and keeps every sentence it carried before

#### Scenario: Quiet levels do not become proactive

- **WHEN** the prominence capture contract is read at `light` or `off`
- **THEN** it is byte-identical to the text it carried before this change

#### Scenario: The compact budget is not raised

- **WHEN** the compact bootstrap payload is measured after the clause lands
- **THEN** it remains under the established ceiling with its warning headroom intact
