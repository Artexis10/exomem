## ADDED Requirements

### Requirement: The engagement guidance teaches reason codes and family dispositions

The bootstrap engagement guidance SHALL teach that a dismissal carries a reason code as the leading token of its why, name the closed vocabulary, and instruct that a user's request to stop hearing about a kind of signal is answered by quieting that family through triage with a reason, never by lowering prominence. The guidance SHALL state that a quiet family is silent on the carriers but still reviewable on request, and that a due-state block omitting a family is not evidence the family is clean. The guidance SHALL stay within the compact bootstrap byte ceiling.

#### Scenario: The bootstrap payload names the vocabulary and the family route

- **WHEN** bootstrap is requested at the compact detail
- **THEN** the engagement guidance lists the reason codes
- **AND** names the family review reference form and the three disposition actions
- **AND** the payload size remains under the compact ceiling
