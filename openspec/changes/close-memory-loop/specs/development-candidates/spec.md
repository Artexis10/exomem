## Purpose

Allow fast isolated development and managed personal candidate testing without requiring a public release for each iteration or weakening state compatibility checks.

## ADDED Requirements

### Requirement: Managed staging accepts immutable local wheel candidates

The staging path SHALL accept an explicit local wheel as an alternative to a published version and record its digest, source revision and actual installed interpreter/package identity. Candidate installation SHALL use a dedicated immutable environment and the existing managed standby, admission, single-writer and cutover protocol. It SHALL NOT mutate the serving interpreter in place, bypass the actual service manager or label an unpublished candidate as a public release.

#### Scenario: A local fix needs personal testing before release

- **WHEN** an isolated tested wheel is selected for candidate staging
- **THEN** the manager admits the exact artifact and interpreter under the same runtime/state checks without waiting for PyPI publication
- **AND** the serving and candidate identities remain inspectable

### Requirement: State compatibility determines candidate admission and recovery

Matching completed state descriptors SHALL reuse existing compatible state without unnecessary migrations. Descriptor-changing candidates SHALL use disposable state or a separately evidenced forward migration and recovery procedure. Supported rollback SHALL require compatible state and runtime authority floors. A migration SHALL NOT imply arbitrary rollback is safe, and an out-of-process index drain SHALL NOT compete with the live service.

#### Scenario: A candidate changes a state descriptor

- **WHEN** a local candidate requires a migration without established recovery evidence for the live state
- **THEN** testing uses isolated disposable state and live admission does not proceed by bypassing compatibility checks

### Requirement: Development and delivery use proportionate verification gates

Development SHALL run tests scoped to affected behaviour in isolated state. Finished feature/tranche delivery SHALL run required full completion checks. Public publication SHALL preserve the repository's release gates and exact-revision evidence while allowing batches of verified changes. Local iteration SHALL NOT require a public release or repeated full-suite runs for unchanged scope. Known failing benchmark/release jobs and deferred paid arms SHALL NOT be rerun merely to occupy the wait for publication.

#### Scenario: A small candidate is revised during development

- **WHEN** a change affects one tested component without expanding the blast radius
- **THEN** the next development pass runs its named affected tests and reserves the full required suite for the completed tranche
- **AND** public release status is reported separately from local candidate readiness
