## ADDED Requirements

### Requirement: A Cloud cell lists two transfer tools with fixed contracts

A Cloud cell SHALL list `observe_transfer` and `transfer_memory`. These two names, their actions and their inputs are a closed protocol set shared with the Cloud gateway.

`observe_transfer` SHALL be read-only (`readOnlyHint` true, `destructiveHint` false, `openWorldHint` false). It SHALL accept `action` (`status`, the default, or `import_handoff`) and an optional `import_id` (a UUID, used by `status` only).

`transfer_memory` SHALL be a write (`readOnlyHint` false, `destructiveHint` true, `openWorldHint` false). It SHALL accept `action` (required: `import_cancel`, `import_discard` or `export_request`), `import_id` (a UUID, required for the `import_*` actions), `export_id` (an optional UUID for `export_request`) and `confirm` (a boolean, default `false`).

Each tool description SHALL teach the workflow: which import routes exist and their limits, how to continue after an import ends, and what an export does. The description MUST NOT retype a numeric limit that another module owns.

#### Scenario: The assistant discovers the tools

- **WHEN** a client lists tools on a Cloud cell
- **THEN** both tools appear with these annotations, closed `action` enums and UUID-typed id inputs
- **AND** no tool accepts a free-form command, path or URL

#### Scenario: The handoff names every route and its limit

- **WHEN** a client reads the description of `observe_transfer`
- **THEN** it states that the command-line route needs a shell, `age` on the path and a browser sign-in
- **AND** it states that the browser route is a desktop upload of folders and that a phone cannot upload a folder
- **AND** it states that chat attachments go through `preserve_artifacts` and `capture_source` within their per-call bounds
- **AND** it states that a finished import is adopted with `adoption_studio` on `_Imports/<import_id>`

### Requirement: The gateway answers the transfer tools only when the deployment enables them

When the Cloud gateway runs with `EXOMEM_CLOUD_TRANSFER_TOOLS_ENABLED=true`, it SHALL answer `tools/call` for exactly these two tools by calling the Substrate web API with the caller's own bearer, after authentication, scope, rate limit and concurrency checks. When the flag is unset, the gateway SHALL pass every call through, and the cell SHALL answer `CLOUD_GATEWAY_REQUIRED`. The platform chart SHALL set the flag only through `cloudGateway.transferTools.enabled`, which defaults to `false`.

Every expected failure SHALL be an application envelope `{success:false, error:{code, message, remediation}}` as a normal tool result. A control-plane failure that is not the caller's SHALL be `CONTROL_PLANE_UNAVAILABLE`.

#### Scenario: The flag is off

- **WHEN** a client calls `observe_transfer` through a gateway without the flag
- **THEN** the call reaches the cell and the result is `CLOUD_GATEWAY_REQUIRED`

#### Scenario: A web refusal reaches the assistant as data

- **WHEN** the web API answers 401, 403 or an error body with a code
- **THEN** the tool result is an application envelope with that code, not an HTTP 401 challenge to the MCP client

### Requirement: Status reports an import and an export that fail independently

`observe_transfer` with `status` SHALL return `{success:true, action:"status", as_of, import, export}`. `import` SHALL be `null`, an import view or an error half. `export` SHALL be an export view, `{state:"unavailable", code:"EXPORTS_NOT_OFFERED"}` when exports are off, or an error half. A failure of one half SHALL NOT hide the other.

For an adoption import that is `done` and not discard-requested, the import view SHALL add `staged_folder` (`_Imports/<import_id>`) and `next` (`adoption_studio`, `action:"start"`, that path). For a `ready` export, the export view SHALL add `download` with the Home URL (fragment `#your-data`) and the `exomem cloud export` argument list.

`import_handoff` SHALL return `{success:true, action:"import_handoff", open_import, routes}`. Routes SHALL be ordered, with `cli` first for a client that identifies as a coding client and `browser` first otherwise. The `attachments` route SHALL take its file and byte bounds from the gateway's artifact-grant constants.

#### Scenario: The import half fails while the export half works

- **WHEN** the imports API fails and the exports API answers
- **THEN** `import` is an error half and `export` is the export view

#### Scenario: A finished adoption import

- **WHEN** an adoption import is `done`
- **THEN** the status names `_Imports/<import_id>` and the exact `adoption_studio` arguments to start on it

### Requirement: Destructive and consent-bound writes require confirmation

`transfer_memory` SHALL make no write call to the control plane for `import_discard` or `export_request` unless `confirm` is `true`. Without it, the result SHALL be `success:false` with `UNCONFIRMED`. For `import_discard` it SHALL carry the import view and state that the staged originals are deleted permanently. For `export_request` it SHALL carry `effects` (`pauses_assistants`, `supersedes_export_id`) and a fresh `export_id`; a confirmed retry with the same `export_id` SHALL be idempotent. `import_cancel` SHALL need no confirmation.

#### Scenario: A discard without confirmation

- **WHEN** an assistant calls `transfer_memory` with `import_discard` and no `confirm`
- **THEN** the result is `UNCONFIRMED` with the import view, and nothing is deleted

#### Scenario: An export request without confirmation

- **WHEN** an assistant calls `transfer_memory` with `export_request` and no `confirm`
- **THEN** the result is `UNCONFIRMED` with the effects and a fresh `export_id`, and no export starts

### Requirement: No tool result carries bytes or links

A transfer tool result SHALL NOT contain a presigned part URL, an import recipient, an export download link, `urls_expire_at`, or any `ciphertext_*` field. Archive bytes SHALL NOT pass through a tool. A result SHALL give the owner's next step as a Home URL or a fixed command.

#### Scenario: A ready export

- **WHEN** an export is ready
- **THEN** the status gives the Home URL and the command that downloads it, and no link that opens the archive

### Requirement: The export command keeps a verified archive or no file

`exomem cloud export --out DIR [--export-id UUID] [--mcp-url URL] [--api-base-url URL]` SHALL authenticate with the OAuth client that `exomem cloud import` uses. It SHALL read the export record, wait for a packing export within a bounded deadline, and request a download link, retrying while the link is pending. It SHALL accept only an `https` link, SHALL refuse any redirect, and SHALL send no OAuth bearer to storage. It SHALL stream to a temporary file in `DIR`, then compare the byte count and sha256 with the export record. It SHALL rename the file into place only when both match, and SHALL delete the temporary file on every failure. It SHALL refuse a record that has no sha256. It SHALL print only JSON with fixed error codes, never the link, and SHALL use a UUID export id for the file name.

#### Scenario: A tampered archive

- **WHEN** the downloaded bytes differ from the recorded digest or size
- **THEN** the command exits with an error code and `DIR` holds no archive and no partial file

#### Scenario: A link that is not plain https

- **WHEN** the issued link uses another scheme, or storage redirects
- **THEN** the command refuses without fetching or following, and `DIR` holds no file

### Requirement: The gateway's public egress is opened only by the transfer-tools flag

The platform chart SHALL render the gateway egress rule for TCP 443 to public addresses only when `cloudGateway.transferTools.enabled` is `true`. The rule SHALL exclude every `cells.jobEgressExcept` range, and the chart SHALL refuse the flag without the private, carrier-grade NAT and link-local ranges in that list or without an enabled gateway. With the flag off, the rendered gateway policy SHALL allow only cell pods on 8765, the control database on 5432 and DNS.

#### Scenario: The flag is off

- **WHEN** the chart renders with the default values
- **THEN** the gateway egress ports are exactly 8765, 5432 and 53

#### Scenario: The flag is on

- **WHEN** the chart renders with the flag on
- **THEN** the gateway egress adds TCP 443 to `0.0.0.0/0` except the private ranges
- **AND** the gateway receives `EXOMEM_CLOUD_TRANSFER_TOOLS_ENABLED=true`
