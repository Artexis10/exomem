## MODIFIED Requirements

### Requirement: Deterministic role selection
Roles for a turn SHALL be selected as the union of the defaults for each resolved
anchor's kind and the roles whose cue patterns match the turn, evaluated with the same
normalisation the anchor resolver uses, with no model call and no randomness; the
selected set SHALL be bounded (at most 6 roles) in the registry's priority order, and
the packet SHALL list the selected roles and, for each, whether it came from an anchor
default or a turn cue. A pathless project SHALL NOT select an effective entity lane
by default, since that lane requires an anchor page. Its declared default SHALL remain
available when an owner override chooses a different lane; cues and defaults from
other anchor kinds SHALL remain unchanged.

#### Scenario: Planning verbs add plan and method roles
- **WHEN** a turn contains a planning cue ("I'm planning to", "how should I") and a
  resource anchor resolves
- **THEN** the selected roles include `resources`, `current_state`, `constraints`
  (anchor defaults) and `active_plans`, `methods` (cues), in registry order

#### Scenario: Same turn, same roles
- **WHEN** the same turn is activated twice against the same index generation and
  registry hash
- **THEN** the selected roles and their attributions are identical

#### Scenario: Project material replaces only an empty default
- **WHEN** an uncued pathless project resolves with shipped roles
- **THEN** its six selected roles include preferences, constraints, recent change,
  active plans, material and precedents, without a page-reading identity lane

#### Scenario: An owner's useful project identity lens survives
- **WHEN** an owner override gives identity a units lane and retains its project default
- **THEN** project resolution still selects identity in the declared priority order

## ADDED Requirements

### Requirement: Explicit material lens for otherwise unowned knowledge
The shipped registry SHALL add `material` after active plans and before methods,
defaulting for project, hub and transient carried-page contexts, with no shipped cues.
It SHALL share the six-role ceiling and existing owner-override rules. A recency-only
context SHALL NOT select it. Existing categorical lanes SHALL remain unchanged.

#### Scenario: Owner narrowing remains effective
- **WHEN** an owner narrows material defaults to an empty set
- **THEN** neither resolved nor carried contexts select material by default

#### Scenario: A full role schedule stays bounded
- **WHEN** six higher-priority roles already qualify
- **THEN** material does not become a seventh lane, and its role-limit omission is visible

### Requirement: Contact units are served only when the turn asks to reach the person

The shipped registry SHALL carry a `contact` role in the `units` lane that selects the open category `contact`, declares no anchor defaults and declares the `contact` intent. A role that declares an intent SHALL be selected only when the turn's analysis tokens (the tokenizer activation already uses) have that intent's shape, whole tokens only, never by a cue substring, never as an anchor default and never merely because a carried page has a free slot. The `contact` intent SHALL be a contact noun (phone, number, email, address, contact) tied to the person, meaning a possessive of the resolved person or a possessive pronoun immediately before the noun, or a whole contact phrase ("phone number", "email address", "mailing address", "contact details"), or a reach verb (call, email, text, message, reach, contact) whose object is the resolved person or a pronoun for them. When selected the role SHALL be attributed `turn_cue`. A vault override SHALL NOT set or change a role's intent, and an unknown intent SHALL be a finding. Units of other categories SHALL NOT be affected, and a page withheld from the caller SHALL contribute no `contact` unit. This role extends the initial vocabulary to fifteen roles.

#### Scenario: A turn about the person without contact intent gets no contact units

- **WHEN** a turn names a resolved person and asks about their work, or says "call it done", "address this issue", "phonetic", "the email thread", "numbered list" or "contact lens"
- **THEN** the packet's selected roles do not include `contact` and no `contact` unit of that person appears

#### Scenario: A turn asking to reach or contact the person gets the contact units

- **WHEN** a turn asks for the resolved person's phone, address or email ("what is Ana's phone", "Ana's address"), or to reach them ("how do I reach Ana", "email Ana about the class")
- **THEN** the packet selects `contact` by `turn_cue` and carries that person's `contact` units

#### Scenario: A carried page never serves contact by rank

- **WHEN** a retrieval-carried page is compiled for a turn without contact intent
- **THEN** the `contact` role is not among its selected lenses, however many `units` slots are free
