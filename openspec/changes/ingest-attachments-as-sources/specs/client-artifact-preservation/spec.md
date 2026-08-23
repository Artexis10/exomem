## MODIFIED Requirements

### Requirement: Capability-driven fallback remains available

The system SHALL retain `transfer_artifact(operation="upload")` and `/upload` for clients that cannot expose attachment handles. Bootstrap, tool descriptions, and scaffold guidance SHALL select the destination lane before the transport: raw material to the Sources capture command and proof-bearing material to the Evidence preservation commands. Having selected the lane, guidance SHALL route capable clients to the file-handle path for that lane and other clients to the existing out-of-band upload path, which SHALL carry the selected lane on the minted capability. Guidance MUST NOT claim token minting proves byte transfer and MUST name `operation`, not the nonexistent `mode` parameter.

#### Scenario: Claude lacks a direct file handle

- **WHEN** a Claude runtime can access an attachment locally but cannot populate the file-handle parameter of the command for the selected lane
- **THEN** guidance directs it to mint an upload capability with `transfer_artifact(operation="upload")` naming that lane, and POST the bytes to `/upload`
- **AND** preservation is considered successful only after the response includes stored path, size, and digest

#### Scenario: ChatGPT sandbox lacks egress

- **WHEN** ChatGPT can populate file parameters but Code Interpreter cannot resolve the Exomem host
- **THEN** guidance directs ChatGPT to call the file-handle command for the selected lane directly
- **AND** no client-side `curl` step is attempted

#### Scenario: An attachment is raw material rather than proof

- **WHEN** an attached transcript, article, or screenshot is being kept as material for later compilation
- **THEN** guidance selects the Sources lane before considering which transport the client supports
- **AND** the artifact is not routed to Evidence merely because the file-handle path was convenient
