## ADDED Requirements

### Requirement: Registry-owned MCP descriptor metadata

The command registry SHALL support immutable optional MCP descriptor metadata and the single generated MCP registration loop SHALL pass that metadata to FastMCP. A command requiring client-specific descriptor extensions MUST remain registry-generated unless its callable schema itself cannot be generated faithfully.

#### Scenario: File parameter metadata reaches discovery

- **WHEN** the generated `preserve_artifacts` MCP tool is listed
- **THEN** its descriptor contains `_meta["openai/fileParams"] == ["files"]`
- **AND** its input schema declares all four supported file properties with only `download_url` and `file_id` required
- **AND** `preserve_artifacts` is absent from the hand-registered exceptions list

#### Scenario: Commands without metadata remain unchanged

- **WHEN** a registry command does not declare MCP descriptor metadata
- **THEN** its generated descriptor and existing schema-fidelity baseline remain byte-identical

### Requirement: Artifact preservation appears in capability guidance

The product command surface SHALL describe `preserve_artifacts` as the canonical binary-evidence capability and `transfer_artifact` as its compatibility transport helper. Full bootstrap guidance SHALL provide copyable direct-file and fallback upload calls and SHALL distinguish obtaining an upload capability from successfully storing bytes.

#### Scenario: Full bootstrap teaches both transport paths

- **WHEN** a client requests the full bootstrap profile
- **THEN** the response shows a `preserve_artifacts(files=[...], scope=..., category=...)` call for clients with file handles
- **AND** it shows `transfer_artifact(operation="upload")` plus `/upload` delivery for clients without file handles
- **AND** neither example embeds binary base64 or a long-lived upload secret
