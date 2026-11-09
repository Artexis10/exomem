## ADDED Requirements

### Requirement: A Presented Credential Is Verified Before A Public Tool Validates Its Arguments

A call to a public tool that presents an authorization session credential SHALL have the credential verified before the tool's arguments are validated and before an unknown-name refusal. An invalid credential SHALL return the credential refusal, never an argument refusal that could confirm which arguments the tool accepts. A call that presents no credential SHALL keep the existing argument refusals.

#### Scenario: An invalid credential is refused before validation
- **WHEN** a client calls a public tool with an invalid credential and malformed arguments
- **THEN** the reply is the credential refusal
- **AND** the reply names no argument error
