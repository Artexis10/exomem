# attention-queue

## ADDED Requirements

### Requirement: Bridge re-reviews surface in the attention queue

The attention/review queue SHALL surface bridges whose `bridge_review` date is due
and bridges flagged for re-review because a `bridge_of` source was restricted or
deleted, as read-only review items citing the exact reason. Acting on such an item
SHALL route through the normal governed review actions and SHALL NOT silently
alter the bridge note.

#### Scenario: Due bridge appears for review

- **WHEN** a bridge's `bridge_review` date has passed
- **THEN** it appears as a review item with its reason, and is not auto-changed

#### Scenario: Source-triggered re-review appears

- **WHEN** a source referenced by an approved bridge is restricted or deleted
- **THEN** the dependent bridge surfaces as a re-review item
