# Shared MCP gateway

The Substrate gateway runs the canonical MCP handler beside tenant cells. The
website, OAuth issuer and public MCP resource remain on Substrate. Deploy this
path only after the paired `simplify-hosted-launch-boundaries` changes have
passed integration acceptance.

## Deployment inputs

- Build Substrate's `Dockerfile.exomem-gateway`; supply its published digest as
  `gateway.image`, using `ghcr.io/artexis10/substrate-gateway@sha256:<digest>`.
- Set `gateway.originHostname` to a dedicated origin on the existing tunnel,
  distinct from the control and browser-transfer hosts. Foundation's optional
  `gateway_hostname` must name the same host. Empty defaults create no route.
- Provide `DATABASE_URL` and `EXOMEM_CONTROL_PLANE_KEY` through the two gateway
  Secret references. The database credential must belong to this gateway's
  required service operations; do not reuse a provider-admin credential or
  copy the complete Vercel environment.
- Resolve the database provider's actual HTTPS and PostgreSQL endpoints and
  populate their IPv4 `/32` addresses in `gateway.databaseEgressCidrs`. These
  addresses are a deployment input, not a permanent provider guarantee:
  re-resolve and update them after provider endpoint changes. Empty lists refuse
  an enabled gateway; broad internet CIDRs are not accepted.
- Confirm the selected deployment lock and protocol match the cell release.
  The chart derives `EXOMEM_CELL_PROTOCOL_VERSION` from that lock, including
  rollback selection. Default concurrency is 16 requests per gateway replica;
  begin with one replica.

## Private acceptance before cutover

Render the chart with the reviewed lock and gateway inputs. Check the selected
image digest, two Secret references, no service-account token, readiness and
liveness probes, CPU/memory limits, and network policy. Validate the foundation
change using the existing reviewed saved-plan workflow; it adds only the
optional DNS record and tunnel ingress, not a Worker or website DNS change.

Deploy the additive runtime and the compatible cell ingress/provisioner update.
Control ingress retains exact v1 paths and permits only POST to the bounded v2
agent-command path. Both platform admission policies accept the old exact v1
route and the new combined route, preserving the rollback window.

Enable the private gateway without setting Substrate's public rewrite. Verify
its direct-origin authentication, ready/probe behavior, trusted control Host,
mapped cell path, private-cell command and rejection of foreign-cell requests.
Gateway egress targets Traefik service port 80, whose destination pod port is
8000; prove that path against the deployed network implementation. Unrelated
pods must not reach the gateway or private cell directly.

The public origin exposes only `/api/exomem/mcp/v1`. Traefik overwrites the
gateway ingress marker and removes caller IP forwarding headers. This is an
aggregate pre-authentication bucket, not an end-user IP identity. Canonical
OAuth and per-principal database limits remain authoritative.

## Public cutover and rollback

After private acceptance, configure Substrate's
`EXOMEM_GATEWAY_TUNNEL_ORIGIN=https://<gateway-origin>`. Its exact-path
`beforeFiles` external rewrite must precede the existing local MCP route in the
built deployment. Verify the actual public path's Authorization/MCP headers,
streaming, cancellation, no-store response headers and disabled rewrite caching;
then run the resumable service acceptance report against the frozen release.
Do not infer authenticated latency or continuity from an unauthenticated 401.

Rollback removes only `EXOMEM_GATEWAY_TUNNEL_ORIGIN` and redeploys Substrate's
original adapter. Keep the public URL, OAuth grants, cell data and v1 route.
Cell rollback uses the existing signed runtime procedure. Do not expand alpha
access until the required service report passes and the previously exposed
provisioning credential has a verified rotation/revocation receipt.
