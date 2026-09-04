## ADDED Requirements

### Requirement: Artifact adoption eligibility does not bypass write authority
Explicit selection, approval, sending, or publication of an offered artifact SHALL establish adoption eligibility but SHALL NOT count as explicit confirmation to write Exomem. Agent-initiated exact-byte Source or Evidence adoption and any later compatible delivery Record SHALL be `proactive_capture` and SHALL obey its active off, advisory, or silent disposition. A user's explicit request to save or preserve the artifact SHALL remain an ordinary requested action and SHALL not create standing authority for later artifacts.

When no compatible Records collection exists, a proposed collection or schema change SHALL be `structural_suggestions`; any later creation or schema mutation SHALL be separate confirmed `restructure_execution`. Adoption success SHALL never depend on that structural work.

#### Scenario: Selection under silent capture preserves exact bytes
- **WHEN** the user selects one offered artifact and `proactive_capture` is `silent`
- **THEN** the active agent may adopt exactly that artifact through the semantically correct Source or Evidence lane
- **AND** the selection grants no authority for sibling or future artifacts

#### Scenario: Advisory capture surfaces before writing
- **WHEN** the user selects one offered artifact and `proactive_capture` is `advisory`
- **THEN** the agent surfaces the proposed exact-byte adoption before invoking the write
- **AND** it does not describe selection alone as write confirmation

#### Scenario: Off capture leaves selected artifact unwritten
- **WHEN** the user selects or approves one offered artifact but `proactive_capture` is `off` and has not explicitly requested preservation
- **THEN** the agent performs no Source, Evidence, adoption-receipt, or delivery-Record write

#### Scenario: Explicit save remains requested action
- **WHEN** the user explicitly asks to save the selected artifact in Exomem
- **THEN** the agent may invoke the correct existing preservation lane as a requested action
- **AND** that request creates no standing delegation for another artifact

#### Scenario: Delivery observation obeys the same capture posture
- **WHEN** a local adoption receipt exists and a definite delivery event is eligible for an existing compatible Records collection
- **THEN** the agent-initiated Record follows `proactive_capture`
- **AND** absence of a compatible collection permits only a structural suggestion, not implicit schema creation
