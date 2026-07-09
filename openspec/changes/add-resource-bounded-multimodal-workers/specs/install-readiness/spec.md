## ADDED Requirements

### Requirement: Standard multimodal product profile
Native release installers SHALL default to a `standard` profile that installs embeddings and
media extras, plus the MLX media extra on Apple Silicon. Existing `lean`, `hybrid`, and `media`
profile names SHALL remain accepted. Missing optional OS-level OCR tooling SHALL be reported
with remediation but MUST NOT prevent the remaining standard service from installing.

#### Scenario: Default release install
- **WHEN** a user runs a native release service command without an explicit profile
- **THEN** the release venv contains the standard embeddings and media extras
- **AND** the installed service uses demand-loaded disposable media workers

#### Scenario: Apple Silicon standard install
- **WHEN** the default release install runs on macOS arm64
- **THEN** the package requirement includes the MLX media extra
- **AND** ASR remains unloaded until media work arrives

#### Scenario: Explicit compatibility profile
- **WHEN** an existing automation selects lean, hybrid, or media
- **THEN** the installer preserves that profile's existing dependency mapping
- **AND** no profile name is silently reinterpreted
