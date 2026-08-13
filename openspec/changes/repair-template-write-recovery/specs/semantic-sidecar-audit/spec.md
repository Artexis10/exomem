## ADDED Requirements

### Requirement: Semantic sidecar census is truthful across supported platforms
The semantic-isolation census SHALL classify each expected sidecar as absent, safely readable, unreadable, schema-unreadable, or platform-unsupported. It SHALL report `sidecar_unreadable` only after an actual safe-open or SQLite-read failure. Platform routing alone SHALL NOT label a sidecar corrupt or unreadable.

#### Scenario: Healthy Windows sidecar is read
- **WHEN** a regular SQLite semantic sidecar and any present companions can be safely bound on Windows
- **THEN** the bounded census queries the sidecar and does not add `sidecar_unreadable` to the incomplete report

#### Scenario: Unsupported binding is distinct from corruption
- **WHEN** the runtime cannot provide the required no-follow binding primitive
- **THEN** the census reports `sidecar_unsupported` and does not claim that stored bytes are unreadable

### Requirement: Windows census pins exact no-follow identities
On Windows, the census SHALL retain no-follow handles for the vault root, Knowledge Base directory, sidecar, and present SQLite companion files with sharing that prevents replacement while permitting bounded SQLite access. It SHALL reject reparse points and unexpected path types, close every handle on all outcomes, and verify stable identities before crediting an exact-row repair.

#### Scenario: Reparse sidecar is rejected without being followed
- **WHEN** a sidecar path or required ancestor is a Windows reparse point
- **THEN** no SQLite connection follows it and the census returns an unsafe/unreadable classification

#### Scenario: Retained reader releases publication promptly
- **WHEN** a bounded census finishes or fails
- **THEN** every retained Windows handle closes and a later legitimate sidecar replacement can proceed

#### Scenario: Repair credit requires the same live entries
- **WHEN** an exact-row repair callback returns changes but the bound sidecar identity no longer matches its live path
- **THEN** Exomem does not report those rows as safely purged
