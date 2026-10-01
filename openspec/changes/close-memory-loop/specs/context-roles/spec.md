## ADDED Requirements

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
