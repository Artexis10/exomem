## Context

The Cloud gateway already authenticates every MCP call and forwards it to the owner's cell. Import and export state lives in the control plane. Home and the CLI already call that web API with the owner's session or OAuth token. The two tools give an assistant the same actions through the connector it already holds.

## Decisions

**D1. The registry says where a command executes.** `Command.executor` is `cell` for every existing command and `control_plane` for the two new ones. The registry derives "Cloud-only" from it, and one filter (`product_commands_for(..., cloud=True)`) builds the Cloud list. Alternatives: a name list in `server.py` (the exact code this replaces), or a separate tool registry (a second source of truth for descriptors, annotations and schemas).

**D2. A cell refuses a control-plane call before any cell work.** The registry wrapper returns the envelope `CLOUD_GATEWAY_REQUIRED` ahead of the read-only gate, the call ledger and the writer lease, so a cell never holds a lease or writes a receipt for work it cannot do. The tools are listed even when the gateway does not serve them, because the gateway decides at call time and a cell cannot know the deployment flag.

**D3. Bytes and links never pass through a tool.** Tools return status, the next step and fixed commands. They never return a presigned part URL, a recipient, a download link or ciphertext metadata. The archive reaches the owner through Home or `exomem cloud export`, which hold the owner's own authority. A transcript that holds a tool result then holds nothing that opens the owner's data.

**D4. Consent is an input, not a gate in prose.** `import_discard` and `export_request` return `UNCONFIRMED` and make no write call unless `confirm` is `true`. The refusal states the effect (permanent deletion, assistants paused, an earlier export superseded). `import_cancel` needs no confirmation: it removes nothing the owner cannot restart.

**D5. The gateway's public egress opens only behind one flag.** The tools need the gateway to call the Substrate web API on a public hostname behind a CDN, so no address can be pinned. With `cloudGateway.transferTools.enabled`, the chart allows TCP 443 to `0.0.0.0/0` except every `cells.jobEgressExcept` range, as the artifact broker's rule does, and refuses the flag without those exceptions. Off, nothing changes, and the gateway keeps its D5 egress (cells on 8765, the control database on 5432, DNS).

**D6. The export command is verify-or-delete.** `exomem cloud export` reuses the import command's OAuth client and endpoint arguments. It sends the OAuth bearer only to the Cloud API. It follows no redirect, accepts an `https` link only, streams to a temporary file in the target folder, compares size and sha256 with the export record, and renames the file into place only after both match. Any failure deletes the partial file. It prints the result as JSON and never the link.

## Risks and trade-offs

- A cell lists tools it cannot run when the flag is off. The refusal carries a remediation, and the flag is off only until the deployment enables it.
- The new egress rule trusts the public internet on 443 for the gateway pod. Private, carrier-grade NAT and link-local ranges stay denied, so cluster, node and metadata addresses stay unreachable. The gateway uses `redirect: "error"`, a timeout and the caller's own bearer.
- The Cloud tool list grows by about 3.2 KB of schema. The Cloud surface has its own budget so the growth is a deliberate, measured number. The bootstrap descriptor grows by 39 bytes.
- The rehearsal environment excepts only the cluster ranges from job egress. It must add the other private ranges before it enables the flag; the chart refuses otherwise.
