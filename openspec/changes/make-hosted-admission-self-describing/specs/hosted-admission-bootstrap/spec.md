## ADDED Requirements

### Requirement: Admission refusal names its own remedy

The system SHALL classify why admission is closed and SHALL make that classification
available to the operator, while the message reaching the refused person remains
non-leaking and unchanged in substance. A refusal caused by the absence of a live
cohort SHALL be distinguishable from every other closure, and its operator-facing form
SHALL reference the bootstrap procedure that clears it.

#### Scenario: No live cohort refuses an invite redemption

- **WHEN** an invited person redeems a valid, unconsumed invitation while no live
  cohort target exists
- **THEN** the refusal is classified as a no-live-cohort closure
- **AND** the operator-facing envelope and structured log name the bootstrap procedure
- **AND** the message shown to the invited person discloses no tenant, cohort,
  candidate, or fleet detail
- **AND** the invitation remains unconsumed, unrevoked, and valid

#### Scenario: A closure with a different cause is not misreported

- **WHEN** admission is refused for a reason other than the absence of a live cohort
- **THEN** the classification names that other cause
- **AND** it does not reference the bootstrap procedure

### Requirement: A fleet that cannot admit anyone is reported as an issue

The system SHALL treat a reconciled fleet with zero bound cells and no live cohort as a
reported inventory issue rather than a clean empty result. Fleet inventory SHALL NOT
report a status that an operator would read as healthy while no tenant can be admitted.

#### Scenario: Empty fleet with no live cohort

- **WHEN** fleet inventory reconciles a fleet with zero bound cells and the control
  plane reports no live cohort
- **THEN** the inventory reports an admission-closed issue
- **AND** the inventory status is not `empty` or `consistent`

#### Scenario: Empty fleet with a live cohort still available

- **WHEN** fleet inventory reconciles a fleet with zero bound cells while a live cohort
  target does exist
- **THEN** the admission-closed issue is not reported

#### Scenario: Upgrade phases refuse to advance into a closed fleet

- **WHEN** a runtime-upgrade execution attempts to advance past inventory while the
  admission-closed issue is present
- **THEN** the phase gate refuses
- **AND** the refusal names the admission-closed issue as the blocking condition

### Requirement: The virgin-install bootstrap is resumable

The system SHALL record bootstrap progress against the reviewer bootstrap authority so
that an interrupted or failed attempt can be resumed. A failed step SHALL NOT consume
the invitation, email alias, staged client release, or client record that the retry
requires, and SHALL NOT leave a tenant that blocks the retry.

#### Scenario: A step fails and the attempt is resumed

- **WHEN** a bootstrap step fails after earlier steps have succeeded
- **THEN** the completed steps remain recorded against the authority
- **AND** the invitation, alias, staged release, and client record remain reusable
- **AND** resuming continues from the first incomplete step

#### Scenario: Resumption re-verifies rather than trusting the checkpoint

- **WHEN** a bootstrap attempt is resumed after the underlying state has changed such
  that a recorded checkpoint is no longer true
- **THEN** resumption refuses
- **AND** it names the precondition that no longer holds

#### Scenario: A stranded tenant does not block a retry

- **WHEN** a bootstrap attempt fails after a tenant has been created
- **THEN** the retry either reuses that tenant or is able to retire it
- **AND** the retry is not refused solely because the earlier attempt created it

### Requirement: Promotion leaves a second platform pairable

The system SHALL retain enough attested bound proof after promoting a candidate for a
further platform to be paired onto that same candidate. Promoting one platform SHALL
NOT permanently foreclose promoting another onto the same candidate.

#### Scenario: A second platform is paired after a single-platform promotion

- **WHEN** a candidate has been promoted for one platform only
- **AND** the operator later supplies complete, valid evidence for a second platform
- **THEN** the pairing is accepted against the retained attested proof
- **AND** it does not require a fresh candidate, reviewer tenant, or evidence run for
  the already-promoted platform

#### Scenario: A promoted candidate is not mistaken for one still rolling out

- **WHEN** any process inspects a candidate that has been promoted while its retained
  proof is still present
- **THEN** the candidate is reported as promoted
- **AND** it is not selected or treated as an in-flight rollout
