<!-- authority:non-specification -->

# Shared MCP gateway

The Substrate gateway runs the canonical MCP handler beside tenant cells. The
website, OAuth issuer and public MCP resource remain on Substrate. Deploy this
path only after the paired `simplify-hosted-launch-boundaries` changes have
passed integration acceptance.

## Deployment inputs

- Publish Substrate's `Dockerfile.exomem-gateway` through its main-only
  `publish-exomem-gateway.yml` workflow after the exact source passes CI. Follow
  Substrate's `docs/runbooks/exomem-gateway-publication.md` to verify the image
  digest and its repository, workflow, source-ref and source-revision provenance.
  Supply that verified digest as `gateway.image`, using
  `ghcr.io/substrate-systems/substrate-gateway@sha256:<digest>`; a source-SHA tag
  is discovery input only. Publishing an image does not authorize deployment.
- Set `gateway.originHostname` to a dedicated origin on the existing tunnel,
  distinct from the control and browser-transfer hosts. Foundation's optional
  `gateway_hostname` must name the same host. Empty defaults create no route.
- Provide `DATABASE_URL` and `EXOMEM_CONTROL_PLANE_KEY` through the two gateway
  Secret references. The database credential must belong to this gateway's
  required service operations; do not reuse a provider-admin credential or
  copy the complete Vercel environment.
  This source delivery leaves the gateway disabled and the existing active
  secret matrix unchanged. Before enablement, register both gateway destinations
  together with their ciphertexts, active selection, handoff tests and signed
  registry in one reviewed deployment change. Do not register an active
  destination without its selected ciphertext.
  Use the existing `secret_handoff.py` workflow: `gateway_database_url` may
  reach only `k3s.gateway.database.active`; the existing `control_plane_key`
  reaches `k3s.gateway.control-plane.active`. Deliver the same wrapping key as
  the canonical Substrate service, not a newly generated unrelated key. Include
  both ciphertexts in the signed active-secret registry before deployment.
  Never create either Kubernetes Secret by hand or print the source value.
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
