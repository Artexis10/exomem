## ADDED Requirements

### Requirement: A cloud cell relays the gateway's service notice

In cloud mode, the cell SHALL read the `x-exomem-service-notice` request header that the authenticated gateway sets. When the header decodes to a valid notice, the cell SHALL add a `service_notice` field, as the result's first field, with its `kind`, `state`, `starts_at`, `ends_at` and `say` values to every MCP tool result that is a JSON object, including JSON-object refusals. The field MUST carry only the header's values and no vault content.

#### Scenario: Recall during scheduled maintenance

- **WHEN** the gateway forwards a recall with a valid notice header for scheduled maintenance
- **THEN** the recall result carries `service_notice` with state `scheduled`, the maintenance times and the `say` sentence

#### Scenario: Refused save during an incident

- **WHEN** a save is refused with a JSON-object refusal while the request carries a valid incident notice
- **THEN** the refusal also carries `service_notice`

#### Scenario: The notice leads the result

- **WHEN** a valid notice reaches a call whose result is a JSON object
- **THEN** `service_notice` is the first field of that object

#### Scenario: A text error carries no notice

- **WHEN** a call is answered with an error that the service returns as text, such as an unknown tool
- **THEN** that error carries no `service_notice`

#### Scenario: The size report excludes the notice

- **WHEN** `activate_context` carries a notice
- **THEN** `budget.serialized_chars` is the length of the packet without `service_notice`

#### Scenario: No notice

- **WHEN** a request carries no notice header
- **THEN** no result carries `service_notice`

### Requirement: The cell ignores an untrusted or malformed notice

The cell SHALL ignore the header outside cloud mode. It SHALL ignore a header that exceeds 1,024 bytes, is not base64url-encoded UTF-8 JSON, has a version other than 1, has a `kind` or `state` outside the published set, or has a `say` longer than 400 characters. An ignored header MUST NOT fail the request.

#### Scenario: Personal service receives the header

- **WHEN** a personal (non-cloud) service receives a request with the header
- **THEN** no result carries `service_notice`

#### Scenario: Malformed header

- **WHEN** a cloud cell receives a header that does not decode to a valid notice
- **THEN** the request succeeds as it would without the header
- **AND** no result carries `service_notice`

#### Scenario: A drifting gateway is visible

- **WHEN** a cloud cell ignores a notice header for the first time since it started
- **THEN** it logs one warning that does not quote the header
- **AND** it logs later ignored headers at debug level
