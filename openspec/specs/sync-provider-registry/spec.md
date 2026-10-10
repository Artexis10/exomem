# sync-provider-registry Specification

## Purpose
Define the evidence that collection store custody uses to find a file-sync root. The evidence is registry data: a shipped pack that a vault overlay can extend but never weaken.

## Requirements

### Requirement: Sync-provider evidence is registry data

Collection store custody SHALL read the evidence of a file-sync root from the
`sync-providers` vocabulary registry: a shipped pack plus the vault overlay. Each entry
SHALL name one evidence kind and its value. Custody SHALL implement each kind and SHALL
NOT branch on an entry key or hold a provider's value in code.

#### Scenario: An owner declares a provider
- **WHEN** the vault overlay adds a Windows sync folder name and the vault lives under a folder of that name on a Windows-mounted path
- **THEN** custody is unverified and its reason names the folder

### Requirement: A vault overlay only extends shipped evidence

The overlay SHALL NOT override, change or retire a shipped entry. A malformed overlay
SHALL leave every shipped entry in force and SHALL report a finding, so no overlay can
weaken custody.

#### Scenario: An overlay retires a shipped provider
- **WHEN** the overlay sets a shipped entry's status to deprecated
- **THEN** the registry reports a finding that names the fixed pack entry
- **AND** custody of a vault inside that provider's folder stays unverified
