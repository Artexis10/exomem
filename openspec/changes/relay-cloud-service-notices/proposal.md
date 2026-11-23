## Why

Exomem Cloud users hear nothing from their assistant about planned maintenance or an incident. When Exomem becomes slow or unavailable, a person can think their memory is lost. The control plane now records operator-declared maintenance and incident notices and passes the current one to each covered cell in a trusted request header. The cell needs to relay that notice in its MCP results, so the connected assistant tells the person.

## What Changes

- In cloud mode only, the cell reads the gateway's `x-exomem-service-notice` header on each request. The value is base64url-encoded UTF-8 JSON with `v`, `kind`, `state`, `starts_at`, `ends_at` and `say`.
- When the header is valid, every MCP tool result that is a JSON object gains a `service_notice` field with those values. This covers recall, search, read and save results, and refusals.
- The cell ignores a missing or malformed header, and a personal (non-cloud) service never reads it.
- The server instructions do not change. They are fixed text that clients read once, so they cannot carry a notice that starts later.

## Capabilities

### Modified Capabilities

- `hosted-tenant-cell`: a cloud cell relays the gateway's service notice in its MCP results.

## Impact

- `src/exomem/cloud_cell.py`: parse and validate the header.
- `src/exomem/command_surface.py`: add the field in the `bind_vault` wrapper after the egress filter, and on the refusal path.
- Tests for the cloud cell. The control-plane side ships in the substrate repository (`add-exomem-service-notices`). A cell without this change ignores the header, so either side can ship first.
