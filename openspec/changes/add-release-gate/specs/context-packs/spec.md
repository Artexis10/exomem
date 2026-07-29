# context-packs

## ADDED Requirements

### Requirement: Deep packs honor release decisions

Deep context packs SHALL be assembled only from items that have passed the release
gate, and each pack element SHALL carry its decision. A pack SHALL NOT contain the
content, claims, neighborhood, or contradictions of any item released below its
excerpt level, and the pack header SHALL carry the governance context (policy
fingerprint and any withheld notices) rather than sub-notice content.

#### Scenario: Withheld item is absent from the pack

- **WHEN** a pack is assembled for a query whose candidates include a withheld item
- **THEN** that item's content, claims, and neighborhood do not appear in the pack,
  and its withholding is represented only by a notice in the pack header

#### Scenario: Permitted pack elements are unchanged

- **WHEN** a pack is assembled with no governed items
- **THEN** the pack is identical to baseline
