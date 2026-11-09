## MODIFIED Requirements

### Requirement: Authoring guidance separates normative rules from live vocabulary

The semantic contract SHALL teach the fixed class rule with a new version and digest.
Admitted status definitions and their identity SHALL remain separate from its normative fields.
Clients SHALL discover current status vocabulary through the exposed registry contract.
Released hosted profiles SHALL retain their historical semantic contract artifacts and descriptors while using current shared runtime semantics.
The versioned bootstrap operating contract SHALL provide separate corrective guidance for the current class rule and classification outcomes.
Full bootstrap SHALL expose that correction without requiring a parameter absent from the frozen descriptor.
Compact bootstrap SHALL expose the current class rule and need not carry the correction's note on historical inactive-label lists, because the compact core is held under a byte ceiling and the released hosted profiles that needed the note are retired.
Registry lookup guidance SHALL name only routes exposed by the active profile.
Unknown labels SHALL create debt only after admitted classification establishes that they are unregistered.

#### Scenario: Frozen clients and current clients receive their own contracts
- **WHEN** a current client and a released hosted profile request their authoring contract
- **THEN** the current client receives the class rule and current registry route
- **AND** the released profile retains its published contract
- **AND** its bootstrap operating contract explains the current shared runtime behavior separately

#### Scenario: A frozen client learns the changed validation rule through its real route
- **WHEN** a v3 client requests full bootstrap and validates a body edit after an owner changes the page's extension class from pending to live
- **THEN** corrective guidance explains that the current live-class rule supersedes historical inactive-label teaching
- **AND** the client's exposed edit route requires a valid semantic unit without changing page bytes during validation
- **AND** bootstrap advertises no registry inspection route that v3 cannot call
