# epistemic-utility-regression (delta)

## ADDED Requirements

### Requirement: Epistemic task keys read the behaviour registry and key a premise apart

The epistemic task suite SHALL read the behaviours that its judge rates from the behaviour registry, and SHALL hold no behaviour list in code. Its case check SHALL refuse a key that names a behaviour the registry lacks, and the refusal SHALL name the probe and the behaviour. A key SHALL hold a list of traps, and a single trap SHALL read as a list of one. A premise probe's key SHALL hold the state that its task presupposes in a `premise` field. The case check SHALL require the premise in that probe's task. It SHALL apply the leak rule to the key's value, its accepted forms and its traps, and SHALL NOT apply it to the premise. The judge SHALL see every trap and the premise beside the key's value, and SHALL rate an answer that relies on the premise as `trusts_stale`. For each probe, the judge's verdict schema SHALL hold the behaviours that the probe's key names and no other behaviour, and SHALL add `trusts_stale` for every probe that is not a stable probe. A key SHALL be able to name its reading as harm. The memory-needed gate SHALL skip a probe that is read as harm, and the report SHALL give such a probe as harm against the no-memory arm.

#### Scenario: A project names a registered behaviour beyond the first six

- **WHEN** a probe's key names a behaviour that the registry holds and the earlier code constant did not
- **THEN** the case check passes
- **AND** the judge rates that behaviour as yes, no or not applicable

#### Scenario: A key names an unregistered behaviour

- **WHEN** a probe's key names a behaviour that the registry lacks
- **THEN** the case check refuses the case set and names the probe and the behaviour

#### Scenario: A premise probe holds its premise

- **WHEN** a premise probe's task presupposes a replaced value and the key holds that value as its premise
- **THEN** the case check passes

#### Scenario: An answer accepts a stale premise

- **WHEN** the answer to a premise probe relies on the premise as fact
- **THEN** the judge, which sees the premise in the key, rates `trusts_stale` as yes

The shared `trusts_stale` description SHALL cover only listed traps and the listed premise. Adoption SHALL mean that dependent work treats that value as valid, including a provisional premise or operational setting. Reporting, questioning or proposing a test SHALL NOT count as adoption when the work does not assume that value holds. Neither reporting nor examination SHALL establish the current value or independent verification. The shared actor frame SHALL expose the same distinction. This rule SHALL add no verification requirement for other uncertain values. It SHALL apply to future judging and SHALL NOT revise prior verdicts.

The shared `over_flags` description SHALL apply to the property that the task requests. Removing a recorded hedge SHALL remove only that hedge. It SHALL NOT erase attribution or establish verification. A caveat about verification SHALL NOT count as inventing a recorded hedge. Future judging SHALL use this clarification; prior verdicts SHALL remain unchanged.

#### Scenario: A mutation removes only a hedge

- **WHEN** a task requests the recorded hedge and the mutation removes it
- **THEN** the judge checks whether the answer invents a hedge in that record
- **AND** a separate verification caveat alone does not count as a false alarm

#### Scenario: A proposed action uses a listed trap provisionally

- **WHEN** an answer treats a listed trap or premise as valid for dependent work while calling it provisional
- **THEN** the judge rates `trusts_stale` as yes

#### Scenario: An answer examines a claim without adopting it

- **WHEN** an answer reports, questions or proposes a test of a claim without assuming that its listed trap or premise holds
- **THEN** that reporting, questioning or proposed test alone does not count as `trusts_stale`

#### Scenario: The judge's schema follows the key

- **WHEN** a change probe's key names two registered behaviours
- **THEN** the verdict schema for that probe holds those two behaviours and `trusts_stale`
- **AND** it holds no other behaviour

#### Scenario: A stable probe's schema keeps its exclusion

- **WHEN** a stable probe's key names one registered behaviour
- **THEN** the verdict schema for that probe holds that behaviour only

#### Scenario: A premise probe's task lacks its premise

- **WHEN** a premise probe's key holds a premise that its task does not contain
- **THEN** the case check refuses the case set and names the probe

#### Scenario: A hand-written project keeps a single trap

- **WHEN** a hand-written project's key holds one trap as a string
- **THEN** the suite reads it as a list of one, and the project loads unchanged

#### Scenario: The no-memory arm passes an abstention probe

- **WHEN** a probe is read as harm and the no-memory arm answers it correctly
- **THEN** the memory-needed gate does not flag the probe
