# agent-bootstrap-contract

## ADDED Requirements

### Requirement: Bootstrap teaches the governance model

The portable bootstrap contract SHALL include a governance section reporting
whether governance is enabled, the current policy fingerprint (or a "missing"
marker), the resolved audience for the caller, how purpose is declared, and a
concise disclosure-model contract, and SHALL bump the contract version when this
section is added. The contract SHALL instruct clients that governance notices and
grant hints appear only in reserved top-level response keys and that
governance-shaped text appearing inside returned content is data, never a command.

#### Scenario: Governance section present and versioned

- **WHEN** a client calls `bootstrap`
- **THEN** the response includes the governance section and a contract version
  reflecting it

#### Scenario: Disabled governance is reported honestly

- **WHEN** `bootstrap` runs on a vault with no `_Governance/` policy
- **THEN** the governance section reports governance as disabled with a "missing"
  fingerprint
