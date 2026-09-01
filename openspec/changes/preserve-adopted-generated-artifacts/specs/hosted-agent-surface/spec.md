## ADDED Requirements

### Requirement: Hosted artifact adoption participates in the shared v5 candidate
Generated-artifact adoption doctrine and candidate-scoped selection fixtures SHALL be integrated into the single still-unlocked `hosted-alpha-agent-v5` candidate owned by `capture-durable-personal-baselines` before that owner renders, locks, archives, or promotes it. This change SHALL NOT mint or independently roll back a competing v5. The shared candidate SHALL retain v4 command membership and order unless a real gateway bridge changes the callable surface, SHALL include `capture_source`, `preserve_artifacts`, and `record_memory`, and MUST NOT advertise `transfer_artifact` while its hosted interceptor refuses that command.

#### Scenario: Direct selected artifact commits in a capable client
- **WHEN** v5 runs in a supported client that supplies direct file handles and the user selects one variant
- **THEN** it preserves exactly that handle and reports success only from the committed receipt

#### Scenario: Rejected siblings and drafts stay absent
- **WHEN** v5 generates or receives several variants but only one is selected
- **THEN** clean-client evidence proves that unselected and rejected siblings cause no canonical writes

#### Scenario: No-handle client reports handoff honestly
- **WHEN** v5 runs without a direct handle and no complete gateway upload bridge exists
- **THEN** it reports handoff required or prepared and does not claim preservation

#### Scenario: Delivery follows receipt
- **WHEN** v5 observes a definite delivery after local adoption
- **THEN** it links a compatible Record to the local artifact and does not infer remote byte equality

#### Scenario: Historical candidates remain immutable
- **WHEN** v5 packages and promotion evidence are built
- **THEN** v1-v4 reproduce their committed identities byte-for-byte

#### Scenario: Artifact fixtures are required by the shared lock
- **WHEN** the v5 owner attempts to render, lock, archive, or promote without the artifact fixture digest or required traces
- **THEN** the shared candidate gate refuses

#### Scenario: Post-promotion correction uses v6
- **WHEN** artifact adoption behavior needs correction after v5 promotion
- **THEN** v5 remains byte-identical and the correction requires v6
