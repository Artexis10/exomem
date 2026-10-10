## ADDED Requirements

### Requirement: A due-state row's fingerprint addresses its own signal

Each reference the due-state block lists SHALL carry its served fingerprint: the stored entry's fingerprint, or for an audience that a policy restricts, the fingerprint recomposed over what that audience may see. A caller that supplies that fingerprint as the expected fingerprint SHALL resolve, for item context and triage, the due signal alone, which is the item the whole-vault resolution already narrows to for that fingerprint. When the served fingerprint is the stored entry's and that entry names one page and one finding, the resolver SHALL answer from that entry and a re-check of its category on that page alone, without a whole-vault audit, and only when the re-check reproduces the stored identity: the same id, a single category and the stored fingerprint. Otherwise it SHALL take the whole-vault path. A reference supplied without that fingerprint SHALL resolve as it does today: the item the review surface lists, with every page-level signal folded into it.

#### Scenario: A due row with its fingerprint is acted on without a whole-vault audit

- **WHEN** an agent reads the context of a due-state row and then dismisses it, each time supplying the row's fingerprint, and the stored entry names one page
- **THEN** neither call runs a whole-vault audit
- **AND** the due-state count stops counting that item

#### Scenario: A bare due reference resolves the item the review surface lists

- **WHEN** a caller reviews and then dismisses a due-state reference without a fingerprint, and the page also carries a page-level signal
- **THEN** the reviewed item carries every category the review surface lists for that page
- **AND** the dismissal removes that item from the open review surface
