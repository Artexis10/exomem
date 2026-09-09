## MODIFIED Requirements

### Requirement: Profile-complete release environment

The release installer SHALL map lean, hybrid, and media profiles to their published
extras, load the selected dotenv file into preflight and service environments, and
MUST NOT depend on the checkout working directory for runtime dotenv discovery.

When it publishes a managed service environment file over an existing one, the
installer SHALL first retain the previous file's contents under a distinct
predecessor path readable only by the current user, and SHALL report that path.
Retention SHALL happen before the replacement becomes visible, so an interrupted
publish never leaves the previous configuration unrecoverable. Configuration
written earlier by the same installer run SHALL NOT be retained, so a predecessor
always holds configuration the run did not author rather than an intermediate
render of its own.

For a service that already exists, the installer SHALL compare the vault path it
is about to render against the vault path recorded in the managed environment
file. When the two differ, the installer SHALL refuse the render, leave the
existing configuration and the running service untouched, and name both paths and
the explicit opt-in required to proceed. When the opt-in is supplied, the
installer SHALL perform the rebinding normally. A first-time render, a service
that does not yet exist, and a managed file recording no vault path SHALL be
unaffected by this refusal.

#### Scenario: Media profile on Apple Silicon
- **WHEN** release mode selects the media profile on macOS arm64
- **THEN** the PyPI requirement includes embeddings, media, vision, diarization,
  and the macOS MLX media extra

#### Scenario: Service receives dotenv values
- **WHEN** the selected `.env` contains Exomem vault and OAuth settings
- **THEN** those values are available to doctor and the installed service
- **AND** generated files containing secrets are readable only by the current user

#### Scenario: Previous managed environment is retained
- **WHEN** the installer publishes a managed service environment file and one already exists
- **THEN** the previous contents are retained under a distinct predecessor path before the
  replacement becomes visible
- **AND** that path is reported and is readable only by the current user

#### Scenario: Vault rebinding is refused for an existing service
- **WHEN** an existing service's managed environment records one vault path and the
  installer is about to render a different one without the explicit opt-in
- **THEN** the installer refuses, naming the recorded path, the rendered path and the opt-in
- **AND** the managed environment file and the running service are left untouched

#### Scenario: Deliberate vault move is permitted
- **WHEN** the same mismatch occurs and the explicit opt-in is supplied
- **THEN** the installer renders the new vault path and proceeds with the transition

#### Scenario: First install is unaffected
- **WHEN** no managed service environment file exists yet, or it records no vault path
- **THEN** the installer renders the selected dotenv without a rebinding refusal
- **AND** no predecessor is left behind, because every file the run replaced was its own
