## ADDED Requirements

### Requirement: Recover exact mutation outcomes across activation acknowledgement loss

Every supported hosted mutator that can publish canonical governance state SHALL prepare deterministic recovery information before its canonical effects and durably bind exact mutation/attempt identity and required effect completion to the committed publication before external acknowledgement can fail. Prepared information alone SHALL NOT establish success. Recovery SHALL verify the committed binding and original private recovery payload, then reconstruct the original canonical outcome under current authorization and egress constraints without reexecuting effectful leaf code. A partial child publication SHALL NOT establish completion of a multi-step command. Historical incomplete attempts lacking sufficient evidence SHALL remain uncertain.

#### Scenario: Process dies after catalog commit
- **WHEN** the process dies after canonical publication but before external acknowledgement or ordinary terminal persistence
- **THEN** the same mutation identity recovers the exact committed publication and original canonical result after authority reconciliation
- **AND** no canonical effect is repeated

#### Scenario: Process dies before canonical commit
- **WHEN** only prepared recovery information exists
- **THEN** recovery cannot report committed success
- **AND** any resumed execution obeys the original effect guards and identity

#### Scenario: Only one child of a composite mutation committed
- **WHEN** a multi-step mutation has a committed child but its required canonical effect set is incomplete
- **THEN** it remains explicitly incomplete and cannot return whole-command success
- **AND** recovery does not repeat completed child effects

#### Scenario: Recovery evidence is substituted
- **WHEN** the payload, request digest, attempt, commit token, attachment, child set or publication binding does not match
- **THEN** recovery refuses without returning another operation's result or changing canonical state

#### Scenario: Legacy abandoned mutation lacks exact proof
- **WHEN** a historical abandoned attempt has a canonical file or catalog publication but lacks the required writer-bound outcome evidence
- **THEN** the system retains its uncertain outcome rather than inferring or fabricating a success receipt

#### Scenario: Maintenance encounters pending writer recovery
- **WHEN** generic governance recovery, artifact cleanup or downmigration encounters a recognized writer recovery journal
- **THEN** it preserves the incomplete mutation and all publication/projection dependencies required for its supported recovery lifetime
- **AND** only the exact recovery owner may advance it; unknown journal variants still fail closed
