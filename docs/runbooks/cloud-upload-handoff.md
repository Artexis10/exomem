<!-- authority:non-specification -->

# Cloud upload handoff configuration

A Cloud cell can issue the same exact-byte upload handoff as a personal server.
The Substrate gateway signs which OAuth login sent each request, and the cell
verifies that proof with the gateway's public key. This runbook covers the cell
side; the gateway side is in the Substrate Cloud operations runbook. It does not
authorize a live rollout.

## What a configured cell does

- It publishes `transfer_artifact`, for `hold=true` uploads only.
- It serves one private `POST /upload` route. The route requires the cell bearer,
  then a gateway proof bound to the exact ticket, before it claims the ticket.
- It refuses the route while `EXOMEM_CLOUD_READ_ONLY` is set, and leaves the
  ticket unspent.
- It keeps every other Cloud exclusion. It serves no `/download`, REST or
  resumable upload route.

## Configuration

One list selects the cells on both sides. Set `cloudGateway.uploadCellIds` in the
private platform values to the JSON array of the cells' 16-character base32 IDs.
Keep the IDs in private values, not in the repository. The chart renders that one
list into both consumers, so the two lists cannot differ:

| Consumer | What the chart renders |
|---|---|
| Gateway | `EXOMEM_CLOUD_UPLOAD_ENABLED=true`, `EXOMEM_CLOUD_UPLOAD_CELL_IDS` with the list, and `EXOMEM_CLOUD_ARTIFACT_SIGNING_KEY` from the private signing-key Secret. |
| cellctl | `CELLCTL_UPLOAD_CELL_IDS` with the same list, and `CELLCTL_UPLOAD_PUBLIC_KEY` from the public-key Secret. |
| Selected cell | `EXOMEM_CLOUD_UPLOAD_PUBLIC_KEY`: the PEM public half of the gateway's Ed25519 artifact signing pair. cellctl sets it only on listed cells. |
| Ingress | One route for `POST /upload` on the gateway host. It adds only the trusted-ingress header and sets no body limit, so the Traefik edge stays a pass-through for large bodies. |

The cell reads the public half from `artifactBroker.publicKeySecretName` /
`publicKeySecretKey`, the Secret that the artifact broker also uses. The private
half goes only to the gateway. A malformed key stops the cell at startup and
names only the variable; cellctl refuses to start on one.

The chart does not check that this Secret exists. Apply it before the upgrade:
cellctl does not start without it. The artifact transport runbook describes its
delivery.

The gateway has no switch for all cells, so uploads follow the list only. A cell
in the list restarts once when the list first names it, and again when the
signing pair rotates. A cell outside the list keeps its pod.

The chart refuses to render a non-empty list unless these hold:

- `cloudGateway.enabled` and `cellctl.enabled` are true.
- When `cloudIngress.enabled` is true,
  `traefik.ports.websecure.transport.respondingTimeouts.readTimeout` is at least
  `900s`. Traefik cuts a request that takes longer than this value to send its
  body, and its default is `60s`. The setting covers every route on the edge
  because Traefik has no timeout for one route. Changing it restarts the Traefik
  pod, which causes a short edge outage.

The read-timeout check runs only when `cloudIngress.enabled` is true. With any
other ingress, set the same timeout there: the chart cannot check it.

This chart does not set the gateway's own body limit or request timeout. The
gateway release must accept a body of at least 101 MiB and allow 900 s for one
request, or a large upload fails at the gateway behind a correct edge.

## Check a selected cell

1. Call `tools/list` through the gateway; expect `transfer_artifact` and no `read_media`.
2. Call `transfer_artifact(operation="upload", lane="source", hold=true, expected_sha256=<digest>)`.
3. Run `exomem attach <file> --handoff-file handoff.json --root <dir>`; expect a held file handle.
4. Call `capture_source` with the returned file and the same digest; expect a stored receipt with that hash.

## Accepted risk: the read timeout covers every edge route

The 900 s read timeout applies to the whole `websecure` entrypoint, not to
`POST /upload`. Traefik maps it to the Go server `ReadTimeout` and sets no
`ReadHeaderTimeout`. So a client that sends request headers slowly probably can
hold an edge connection for up to 900 s on any route, not only on the upload
route. We have not measured this. We accept the availability risk for now.

The exit is a dedicated upload entrypoint with its own timeout. That needs a
gateway change, because the gateway must build `upload_url` on another origin.

## Undo

1. Remove the cell from `cloudGateway.uploadCellIds` and upgrade the chart; the gateway stops selecting it, so new uploads to it fail.
2. Wait for cellctl to restart the cell; `transfer_artifact` is withdrawn.
3. Empty `cloudGateway.uploadCellIds` to withdraw the handoff everywhere; the chart then renders as it did before.
4. Emptying the list does not revert the 900 s read timeout. Remove `traefik.ports.websecure.transport.respondingTimeouts.readTimeout` from the private values, or set the earlier value, and upgrade the chart. The Traefik pod restarts, which causes a short edge outage.

Existing capture receipts stay valid. Unused holds and tickets expire through
their normal lifetimes.
