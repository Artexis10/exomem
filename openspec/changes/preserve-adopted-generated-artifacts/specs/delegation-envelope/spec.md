## ADDED Requirements

### Requirement: Handing over an artifact does not bypass write authority
Handing a generated file to the user, or a definite report that an offered artifact was sent or published, SHALL be the adoption trigger but SHALL NOT count as explicit confirmation to write Exomem. Agent-initiated exact-byte Source or Evidence adoption and any later compatible delivery Record SHALL be `proactive_capture` and SHALL obey its active off, advisory, or silent disposition. A user's explicit request to save or preserve the artifact SHALL remain an ordinary requested action and SHALL not create standing authority for later artifacts.

When no compatible Records collection exists, a proposed collection or schema change SHALL be `structural_suggestions`; any later creation or schema mutation SHALL be separate confirmed `restructure_execution`. Adoption success SHALL never depend on that structural work.

#### Scenario: Hand-over under silent capture preserves exact bytes
- **WHEN** the agent hands the user one generated file and `proactive_capture` is `silent`
- **THEN** the active agent adopts exactly that file through the semantically correct Source or Evidence lane
- **AND** the hand-over grants no standing authority for files the agent never hands over or for future files

#### Scenario: Advisory capture surfaces before writing
- **WHEN** the agent hands the user one generated file and `proactive_capture` is `advisory`
- **THEN** the agent surfaces the proposed exact-byte adoption before invoking the write
- **AND** it does not describe the hand-over alone as write confirmation

#### Scenario: Off capture leaves a handed-over file unwritten
- **WHEN** the agent hands the user one generated file but `proactive_capture` is `off` and the user has not explicitly requested preservation
- **THEN** the agent performs no Source, Evidence, adoption-receipt, or delivery-Record write

#### Scenario: Explicit save remains requested action
- **WHEN** the user explicitly asks to save a generated file in Exomem
- **THEN** the agent may invoke the correct existing preservation lane as a requested action
- **AND** that request creates no standing delegation for another file

#### Scenario: Delivery observation obeys the same capture posture
- **WHEN** a local adoption receipt exists and a definite delivery event is eligible for an existing compatible Records collection
- **THEN** the agent-initiated Record follows `proactive_capture`
- **AND** absence of a compatible collection permits only a structural suggestion, not implicit schema creation
