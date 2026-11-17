## ADDED Requirements

### Requirement: A note write names the record it did not file

When a committed note write's stored terminal carries a records disposition that files a new item, appends an occurrence or asks the user, `user_summary` SHALL add "; not filed in <collection title>" before the store name.
`next_action` SHALL carry that disposition's ready call, or its question when the disposition asks.
A disposition that holds for review, a moderate route without a disposition, or a write without records routing SHALL leave the sentence unchanged.
The text SHALL come only from the stored terminal, so a replay returns the same sentence.

#### Scenario: A diagnosis note belongs in an incident collection

- **WHEN** an agent saves a failure note that routes strongly to an incident collection whose disposition files the item
- **THEN** `user_summary` reads "Saved -> <note title>; not filed in <collection title> (<store name>)"
- **AND** `next_action` is the ready call that files the item

#### Scenario: The policy holds filing for review

- **WHEN** the disposition holds for review
- **THEN** `user_summary` reads "Saved -> <note title> (<store name>)"
