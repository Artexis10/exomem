# Spec Delta

## ADDED Requirements

### Requirement: The default query_records reply carries one copy of its rows

A `query_records` reply SHALL include governed rows and metadata and omit `rendered` unless the caller requests a supported `options.output_format`. Typed and derived replies, direct `record_memory` replies, query bounds and disclosure checks SHALL retain their current contracts.

#### Scenario: A default query returns structured rows
- **WHEN** `query_records` reads a collection without `options.output_format`
- **THEN** the reply contains the released rows and their normal identifiers, provenance and continuation
- **AND** it omits `rendered`

#### Scenario: An export specifies a format
- **WHEN** the caller requests JSON, Markdown or CSV with `options.output_format` on a query supporting that format
- **THEN** the reply includes the requested rendering and the governed structured result
