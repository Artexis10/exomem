## Why

Exomem Cloud will hold private vaults: the owner's first, then friends'. The pre-deploy security review of the production render (2026-09-27) found that Traefik, which terminates public TLS on the node, can read every Secret in the cluster. So can the operator's everyday kubeconfig. A proxy compromise, or a mistyped operator command, can therefore reach tenant keys and live vault content.

Tenants are already isolated from each other. What is missing is keeping the fleet's durable keys out of reach of the internet-facing edge, and tenant content out of the operator's accidental reach, without giving up server-side search, embeddings or review. The edge terminates TLS, so it will always see live traffic; the point is that compromising it must not yield keys that unlock every cell, every backup and every future session. This must land before any real vault or friend is admitted.

## What Changes

- **Edge isolation.**
  - Traefik moves to a dedicated edge namespace, with namespaced RBAC that watches only that namespace. The namespace enforces the privileged Pod Security level, because Traefik binds hostPort 443; restricted runs in audit and warn modes.
  - The public Cloud route moves there too: IngressRoute, trusted-ingress Middleware, Certificate and TLS Secret.
  - The only Secret in the edge namespace is the TLS certificate. The route reaches the gateway through an ExternalName Service.
  - The DNS-01 credential moves to a ClusterIssuer outside the edge namespace. An admission policy lets only the edge namespace reference that ClusterIssuer.
  - The gateway admits ingress only from the edge namespace's Traefik pods.
- **Operator identity split.**
  - Day-to-day operation uses a restricted kubeconfig. It can read workload status, events and content-free logs. It cannot read Secrets, and it cannot exec, attach, port-forward or proxy into cell namespaces.
  - The root-only admin kubeconfig becomes break-glass. The existing K3s audit policy records every use.
- **Break-glass on demand.** A break-glass certificate is minted through an audited CSR, valid for about one hour. The everyday operator certificate is short-lived too.
- **Proof that logs stay content-free.** A live test seeds a canary string, exercises search and review, and asserts the canary never reaches cell or controller logs.
- **Break-glass admission (conditional).** A live-K3s test must first prove that a ValidatingAdmissionPolicy can match CONNECT on the relevant pod subresources. If it can, the policy denies `pods/exec`, `pods/attach`, `pods/portforward` and `pods/ephemeralcontainers` in cell namespaces to everyone outside a break-glass group. If the test shows it cannot, the identity split stands alone and the design records why.
- **Tenant-only exports.** An operator export is encrypted only to the requesting tenant's own age recipient. The operator never holds the private key and never sees plaintext.
- **Truthful privacy statement.** It asserts:
  - tenants cannot reach one another;
  - the internet-facing edge cannot read durable tenant keys;
  - the operator cannot see content by accident, and deliberate access leaves an audit trail on the node.

  It discloses:
  - the edge and the gateway see requests and responses in transit;
  - the operator holds the volume and backup keys on the serving node, and could deliberately read content.

  Key separation is deferred.

## Capabilities

### New Capabilities
- `cloud-operator-access`: Exomem Cloud's protection of tenant content from the public edge and from routine operator access. Covers edge isolation, the restricted operator identity, break-glass access and its audit trail, tenant-only export encryption, and the privacy statement the product may make.

### Modified Capabilities

None. The `cloud-cell` capability is still an unarchived delta in `adopt-exomem-cloud-plain-cells`. This change adds a separate capability and does not alter those requirements.

## Impact

- `infra/helm/platform`: Traefik subchart values (namespace override, namespaced RBAC, providers, ExternalName services). `templates/cloud-ingress.yaml` (edge namespace, ClusterIssuer, ExternalName Service). `templates/cloud-gateway.yaml` (NetworkPolicy peer). A new operator RBAC template. An optional break-glass ValidatingAdmissionPolicy.
- `infra/cellctl/tests` chart and live-K3s tests; `tests/test_hosted_*` render contracts.
- `docs/runbooks/cloud-operator-export.md`: the recipient must be the tenant. A new operator-access runbook covers minting the restricted kubeconfig and using break-glass.
- **Legacy hosted platform.** Moving Traefik out of `exomem-platform` changes the Service address that the legacy `cloudflared` route dials. The legacy platform is paused, with zero cells, pending retirement. This change accepts that the paused legacy web route stops resolving.
- **Deployment.** One Helm upgrade to the live `exomem-platform` release, plus a kubeconfig handover on the node. It runs before any real vault import or friend admission. It is not needed for the empty-data smoke test.
