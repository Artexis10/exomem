## ADDED Requirements

### Requirement: Media Processing Accepts A Language Mode

`process_media` SHALL accept `language` and `multilingual` on the `process` and `reprocess` operations, with the same meaning on MCP, REST and CLI. A language or mode applies to speech media only; on other media the request SHALL be refused with a reason.

#### Scenario: Parity across surfaces

- **WHEN** a caller sends `process` with `multilingual: true` through MCP, REST or CLI
- **THEN** each surface queues the same job with the multilingual mode

#### Scenario: A language mode on a document

- **WHEN** a caller sends `process` with a language for a PDF
- **THEN** the request is refused with a reason that a language applies to speech media only
