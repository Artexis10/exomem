# epistemic-utility-regression (delta)

## ADDED Requirements

### Requirement: Epistemic task keys read the behaviour registry and key a premise apart

The epistemic task suite SHALL read the behaviours that its judge rates from the behaviour registry, and SHALL hold no behaviour list in code. Its case check SHALL refuse a key that names a behaviour the registry lacks, and the refusal SHALL name the probe and the behaviour. A key SHALL hold a list of traps, and a single trap SHALL read as a list of one. A premise probe's key SHALL hold the state that its task presupposes in a `premise` field. The case check SHALL require the premise in that probe's task. It SHALL apply the leak rule to the key's value, its accepted forms and its traps, and SHALL NOT apply it to the premise. The judge SHALL see every trap and the premise beside the key's value, and SHALL rate an answer that relies on the premise as `trusts_stale`. For each probe, the judge's verdict schema SHALL hold the behaviours that the probe's key names and `trusts_stale`, and no other behaviour. A key SHALL be able to name its reading as harm. The memory-needed gate SHALL skip a probe that is read as harm, and the report SHALL give such a probe as harm against the no-memory arm.

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

#### Scenario: The judge's schema follows the key

- **WHEN** a probe's key names two registered behaviours
- **THEN** the verdict schema for that probe holds those two behaviours and `trusts_stale`
- **AND** it holds no other behaviour

#### Scenario: A premise probe's task lacks its premise

- **WHEN** a premise probe's key holds a premise that its task does not contain
- **THEN** the case check refuses the case set and names the probe

#### Scenario: A hand-written project keeps a single trap

- **WHEN** a hand-written project's key holds one trap as a string
- **THEN** the suite reads it as a list of one, and the project loads unchanged

#### Scenario: The no-memory arm passes an abstention probe

- **WHEN** a probe is read as harm and the no-memory arm answers it correctly
- **THEN** the memory-needed gate does not flag the probe
