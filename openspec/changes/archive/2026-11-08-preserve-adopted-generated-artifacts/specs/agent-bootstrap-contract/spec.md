## ADDED Requirements

### Requirement: Bootstrap teaches generated artifact adoption
Balanced and maximal bootstrap profiles SHALL teach that the agent keeps the exact bytes of every generated file it hands the user, at hand-over, and that drafts it never hands over stay ephemeral. They SHALL teach that semantic Source-or-Evidence choice precedes transport and that success requires a committed local receipt. They SHALL run the hand-over rule under the current `proactive_capture` disposition, so a hand-over is a trigger and not a standing write permission, and route agent-initiated delivery through the same disposition. They SHALL teach that delivery is a separate observed Record and remote byte equality cannot be inferred. A filtered surface without direct handles SHALL expose an honest unavailable or handoff state and MUST NOT advertise a nonexistent callable route. Off and light profiles SHALL retain their existing explicit-request-only semantics. The scope boundary (files the agent writes or edits in the user's own workspace or repository are not covered) SHALL live in the skill's engagement reference; the compact bootstrap need not carry it.

#### Scenario: Direct-capable bootstrap names exact receipt boundary
- **WHEN** bootstrap is rendered for a profile exporting direct client-file preservation
- **THEN** it teaches hand-over adoption, exact-byte commit, draft silence, and receipt-gated success

#### Scenario: Filtered bootstrap is capability-honest
- **WHEN** bootstrap is rendered for a profile without direct file handles
- **THEN** it teaches a non-committing handoff or unavailable state and names no unavailable command

#### Scenario: Delivery remains separate
- **WHEN** bootstrap teaches sent or published output handling
- **THEN** it requires a local receipt first and forbids an unsupported remote byte-identity claim

#### Scenario: Compact contract stays bounded
- **WHEN** adoption doctrine is added to compact bootstrap
- **THEN** it appears once without increasing the established byte budget absent a separate decision
