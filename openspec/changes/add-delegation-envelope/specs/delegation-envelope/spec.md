## ADDED Requirements

### Requirement: Hard authority ceilings bound every action class

The product SHALL define a closed v1 set of envelope action classes —
`hygiene_writes`, `proactive_capture`, `link_acceptance`,
`structural_suggestions`, `restructure_execution`, `disclosure` — each with a
hard ceiling: `hygiene_writes` silent; `proactive_capture` silent-capable;
`link_acceptance` confirm; `structural_suggestions` advisory (surface only);
`restructure_execution` — covering restructure application, supersession
commit, entity creation, and deletion — confirm-required; `disclosure` governed
exclusively by the governance plane. No prominence level, envelope setting,
disposition, configuration value, or adaptation SHALL authorize behaviour above
a class's ceiling. An unknown class id, and any attempt to configure
`disclosure` through the envelope, SHALL be refused with a named error and no
state change.

#### Scenario: Maximal prominence cannot lift a ceiling

- **WHEN** prominence is `maximal` and every envelope override is set as
  permissive as its range allows
- **THEN** restructure application, supersession commit, entity creation, and
  deletion still each require explicit confirmation before executing

#### Scenario: An unknown class is refused

- **WHEN** an envelope disposition is requested for a class id outside the
  closed v1 set
- **THEN** the request is refused with a class-specific error and no state
  changes

#### Scenario: Disclosure is not envelope-configurable

- **WHEN** an envelope disposition is requested for `disclosure`
- **THEN** the request is refused naming the governance plane as the owner

### Requirement: The envelope derives from prominence with explicit overrides

Each action class SHALL carry a disposition drawn from its class range:
`hygiene_writes` {silent}; `proactive_capture` {off, advisory, silent};
`link_acceptance` {off, advisory, confirm-shortcut}; `structural_suggestions`
{off, advisory}; `restructure_execution` {confirm}. Absent an explicit
override, the disposition SHALL be a pure derivation from the active prominence
level: `hygiene_writes` silent at every level; `proactive_capture` off/off/
silent/silent for off/light/balanced/maximal; `link_acceptance` and
`structural_suggestions` off at prominence `off` and advisory otherwise;
`restructure_execution` confirm at every level. An explicit override SHALL
persist across engine restarts and prominence changes until reset, SHALL be
stored in the shared engagement configuration, and SHALL be refused when
outside the class range. The active envelope — every class, its disposition,
and whether it is derived or overridden — SHALL be inspectable from the same
surface that lists family dispositions and SHALL be served to the agent by
bootstrap. A reset SHALL restore pure derivation for the named class.

#### Scenario: An override outlives a prominence change and a restart

- **WHEN** `proactive_capture` is explicitly set `advisory` while prominence is
  `maximal`, prominence is then changed to `balanced`, and the engine restarts
- **THEN** the served envelope still carries `proactive_capture: advisory`,
  marked as an override, and every other class re-derives from `balanced`

#### Scenario: Out-of-range dispositions are refused

- **WHEN** `structural_suggestions` is set to `silent`, or
  `restructure_execution` to anything but `confirm`
- **THEN** each request is refused naming the class's range and nothing changes

#### Scenario: Reset restores derivation

- **WHEN** the `proactive_capture` override is reset at prominence `balanced`
- **THEN** the served disposition returns to `silent` and is marked derived

### Requirement: Envelope adaptation is deterministic and consent-shaped

Envelope and family-disposition adaptation SHALL derive only from explicit
triage decisions. When review-state records show three dismissals of items in
one registered family with no intervening quiet offer, the next surfacing of
that family SHALL carry exactly one offer to quiet it; the offer SHALL be
recorded durably against the family, made at most once per family until the
family is reset to `normal`, and SHALL change nothing by itself. Usage logs,
query history, read counts, and any engagement measure SHALL NOT be inputs to
any adaptation. Nothing SHALL be quieted, turned off, or made more permissive
except by an explicit decision.

#### Scenario: Three dismissals earn one offer, once

- **WHEN** a user dismisses three items of one family and then dismisses two
  more without acting on the offer
- **THEN** the family's next surfacing after the third dismissal carried the
  quiet offer, and no later surfacing repeats it

#### Scenario: Nothing adapts on its own

- **WHEN** a family's items are repeatedly surfaced and ignored without triage
- **THEN** the family's disposition and every envelope cell are unchanged

### Requirement: Standing delegation is not a v1 envelope cell

The envelope SHALL NOT offer, accept, or store a standing delegation of
`restructure_execution` — any request shaped as "always allow" or "do this kind
of thing from now on" for that class SHALL be refused with a named error
stating that standing delegation is gated on an explicit founder ratification
and does not exist in v1.

#### Scenario: A standing-delegation request is refused by name

- **WHEN** any surface attempts to set `restructure_execution` to a
  non-confirm disposition, however phrased
- **THEN** the refusal names the founder gate and the envelope is unchanged

### Requirement: The envelope round-trips on a hookless client

On a client with no hooks installed, a plain-language request to stop a kind of
suggestion SHALL durably quiet exactly the named family through the existing
family-disposition surface; the change SHALL be inspectable with its reason and
origin, SHALL persist across sessions and engine restarts, and SHALL reset on
request — with the envelope's class dispositions unaffected throughout.

#### Scenario: "Stop suggesting projects" sticks, is visible, and resets

- **WHEN** a hookless session asks to stop project-structure suggestions, a new
  session starts, and the dispositions view is read
- **THEN** the structural family the request named is `quiet` with the recorded
  reason and origin, other families are untouched, and a reset request restores
  `normal`
