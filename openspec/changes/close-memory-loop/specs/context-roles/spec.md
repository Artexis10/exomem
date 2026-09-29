## ADDED Requirements

### Requirement: Contact units are served only when the turn asks for them

The shipped registry SHALL carry a `contact` role in the `units` lane that selects the open category `contact` and declares no anchor defaults, so an entity anchor never pulls its `contact` units into a packet by itself. The role SHALL be selected only by a turn cue naming an intent to reach the person (a phone, a call, an email, an address, contact details); when selected it SHALL be attributed `turn_cue`. Units of other categories SHALL NOT be affected, and a page withheld from the caller SHALL contribute no `contact` unit. This role extends the initial vocabulary to fifteen roles.

#### Scenario: A turn about the person without contact intent gets no contact units

- **WHEN** a turn names a resolved person and asks about their work, not how to reach them
- **THEN** the packet's selected roles do not include `contact` and no `contact` unit of that person appears

#### Scenario: A turn asking for their phone gets the contact units

- **WHEN** a turn asks for the resolved person's phone number
- **THEN** the packet selects `contact` by `turn_cue` and carries that person's `contact` units
