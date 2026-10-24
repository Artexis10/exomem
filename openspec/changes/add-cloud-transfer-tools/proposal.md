## Why

An owner moves a vault into or out of Exomem Cloud with Home, or with the `exomem cloud import` command. An assistant on the owner's cell cannot see the transfer, cannot start the right route, and cannot cancel, discard or request an export. The owner must leave the conversation to find out what to do.

A cell cannot serve these actions. They run against the control plane (the Substrate web API), and a cell holds no credential for it. The owner's assistant already reaches the Cloud gateway, which holds the owner's session and can call that API.

## What Changes

- **Cloud-only MCP tools.** A Cloud cell lists `observe_transfer` (read-only) and `transfer_memory` (write, destructive). No other surface lists them.
- **Control-plane executor.** The command registry gains a closed attribute `executor` (`cell` or `control_plane`). A call that reaches a cell for a `control_plane` command gets the application envelope `CLOUD_GATEWAY_REQUIRED`. The cell takes no writer lease, writes no receipt and reads no vault.
- **One Cloud-surface filter.** The registry builds the Cloud tool list from `CLOUD_SURFACE_EXCLUSIONS` and the `executor` attribute. The server no longer tests command names.
- **Export download command.** `exomem cloud export --out DIR` downloads the owner's ready export over OAuth. It keeps the archive only when its size and sha256 match the export record.
- **Gateway egress, behind a flag.** The platform chart gains `cloudGateway.transferTools.enabled`, default `false`. On, the gateway may reach the Substrate web API on TCP 443 and sets `EXOMEM_CLOUD_TRANSFER_TOOLS_ENABLED`. Off, the rendered chart is unchanged.
- **Cloud surface size budget.** The tool-schema budget now measures the Cloud surface as well as the local one.

The Substrate gateway answers the two tools; that change belongs to the Substrate repository. This change fixes the contract it builds to.

## Capabilities

### New Capabilities

- `cloud-transfer-tools`: the two tools' names, inputs, annotations and result rules, the gateway-served flag, and the export download command.

### Modified Capabilities

- `command-surface`: adds the `executor` attribute, the Cloud-only listing and the `CLOUD_GATEWAY_REQUIRED` refusal.
- `cloud-cell` and `cloud-artifact-transport`: not canonical yet, so the amendments are made in place, dated, in the active changes `adopt-exomem-cloud-plain-cells` and `serve-cloud-artifact-handles`. They cover the Cloud tool surface and the gateway's public egress. This change carries no delta for either.

## Impact

- **Exomem (`src/exomem`):** `command_surface.py` (`executor`, the refusal), `commands.py` (two registry rows and leaf docstrings), `server.py` (one filter call), `cloud_export_client.py` and `cloud_import_client.py` (shared OAuth helpers), `__main__.py` (`cloud export`), the governance coverage lists.
- **Fixtures and budgets:** a new Cloud-only schema fixture; the Cloud surface budget. The local tool fixture and fingerprint are unchanged.
- **Platform chart:** `cloudGateway.transferTools.enabled`, one NetworkPolicy rule and one env variable, both only when the flag is on. No image digest changes.
- **Substrate (its own change):** the gateway intercept, the export display's `archive_sha256`, and the web API the tools call.
- **Privacy:** no tool result carries a presigned URL, a recipient, a download link or ciphertext metadata. The export link reaches only the command that asked for it.
