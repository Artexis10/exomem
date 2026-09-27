## Context

The platform chart runs Traefik as a subchart in the `exomem-platform` release namespace. Its ClusterRole reads Secrets in every namespace. The Cloud route (IngressRoute, Middleware, Issuer, Certificate, TLS Secret) lives in `exomem-cloud`, beside cellctl's and the gateway's keys: the cell token key, backup master key, B2 key-management credential and database DSNs. `exomem-platform` holds the Cloud volume-encryption passphrase and the legacy platform's Secrets. The operator uses K3s's admin kubeconfig (`system:masters`) for everything.

The node already runs a K3s audit policy (`infra/ansible/roles/k3s/files/audit-policy.yaml`). It records every request at Metadata level, and Secrets at Metadata only, retained 7 days on the node.

cellctl's own writes are already confined by ValidatingAdmissionPolicies. Tenant-to-tenant isolation is out of scope here; `cloud-cell` covers it.

## Goals / Non-Goals

**Goals:**
- A compromised edge proxy yields at most the public certificate.
- No operator command run through the everyday identity can reach vault content or tenant keys.
- Any deeper access is deliberate, uses a distinct identity, and is audited.

**Non-Goals:**
- Hiding content from root on a running node. That requires key separation or client-side encryption, which is deferred.
- Shipping audit logs off the node.
- Changing any cell-side feature.

## Decisions

### D1. Traefik moves to its own namespace inside the same release

Set the Traefik subchart's `namespaceOverride: exomem-edge`, `rbac.namespaced: true`, and `providers.kubernetesCRD.namespaces: [exomem-edge]`. Its Role then grants Secret reads only in `exomem-edge`. The platform chart creates the `exomem-edge` namespace with restricted Pod Security labels.

- *Rejected: namespaced RBAC in place.* Traefik always watches its own release namespace, and `exomem-platform` holds the volume-encryption passphrase. The Cloud route namespace holds every Cloud key.
- *Rejected: a separate Helm release for Traefik.* It adds a second release lifecycle for no isolation gain over `namespaceOverride`.

### D2. The route reaches the gateway through an ExternalName Service

The IngressRoute and trusted-ingress Middleware move to `exomem-edge`. An ExternalName Service there points at `exomem-cloud-gateway.<cellctl.namespace>.svc.cluster.local`, with `providers.kubernetesCRD.allowExternalNameServices: true`. The gateway's NetworkPolicy admits ingress only from pods labelled `exomem.io/ingress: traefik` in the `exomem-edge` namespace.

- *Rejected: `allowCrossNamespace`.* Traefik would have to watch `exomem-cloud`, and namespaced RBAC would then grant it that namespace's Secrets.

### D3. DNS-01 runs through a ClusterIssuer whose credential stays in `exomem-cloud`

The Certificate moves to `exomem-edge`, and the TLS Secret it produces lands there. It names a ClusterIssuer. cert-manager's `clusterResourceNamespace` is set to `cellctl.namespace`, so the existing `exomem-cloudflare-dns-token` destination stays where the signed secret registry already places it. The edge never sees the DNS-edit token.

- *Rejected: an Issuer in `exomem-edge`.* The token would sit beside Traefik, and a DNS-edit token is enough to hijack the hostname.
- *Rejected: moving the token's destination.* That changes the signed registry and needs a re-sign, for no gain.

### D4. Three identities, each with one purpose

1. **Everyday operator (`exomem-operator`).**
   - A client certificate issued through the Kubernetes CSR API (signer `kubernetes.io/kube-apiserver-client`), in group `exomem:operators`.
   - Bound to a ClusterRole with get/list/watch on pods, `pods/log`, events, namespaces, deployments, statefulsets, jobs, PVCs, services and nodes.
   - No Secrets and no connect subresources.
   - Its kubeconfig is the default in every runbook.
2. **Deploy (K3s admin kubeconfig, root-only).** Used by scripted Helm and apply procedures, which need broad write rights.
3. **Break-glass (`exomem-break-glass`).** A separate client certificate in group `exomem:break-glass`, root-only, never in any runbook's default path.

- *Rejected: using the admin kubeconfig day to day.* Admin can read every Secret and exec anywhere, so a mistyped namespace becomes content access.

### D5. Admission denies connect subresources in cell namespaces to everyone but break-glass

A ValidatingAdmissionPolicy matches CONNECT on `pods/exec`, `pods/attach` and `pods/portforward`, and UPDATE on `pods/ephemeralcontainers`, in namespaces matching `^exo-cell-[a-z2-7]{16}$`. It denies these unless `request.userInfo.groups` contains `exomem:break-glass`. Admission applies to `system:masters` too, so the deploy identity cannot open a shell in a cell either.

The chart ships this only after the live-K3s suite proves the API server matches these CONNECT operations under this policy type. If it does not, the policy is left out, the identity split in D4 stands alone, and this design records the observed behaviour.

- *Rejected: an admission webhook.* It adds a service to run and fail closed, for the same effect.

### D6. The export runbook refuses operator-held recipients

`docs/runbooks/cloud-operator-export.md` already streams the vault only through age to a recipients file. It gains two rules:
- the recipients file must be supplied by the tenant, with its fingerprint recorded before the restore;
- the procedure stops if any listed recipient matches a registered operator or escrow key.

### D7. The audit policy already satisfies the break-glass record

Metadata-level logging captures the user, groups, verb, resource, subresource, namespace and time for every request, including exec, with no bodies. A task confirms this on the live node. Changing retention is out of scope.

## Risks / Trade-offs

- [Moving Traefik changes its Service address, so the paused legacy `cloudflared` route stops resolving] → Accepted. Legacy is paused with zero cells and is being retired. The change notes it, and nothing Cloud-facing uses that route.
- [There is a brief 443 outage while Traefik moves and the Certificate re-issues in `exomem-edge`] → Run it before any real vault is imported. DNS-01 issuance takes minutes.
- [ExternalName routing hides a gateway outage behind DNS] → The gateway readiness probe and a post-deploy smoke check through the public route catch it.
- [The CSR-issued operator certificate expires] → The operator-access runbook records the expiry and re-issue steps. An expired certificate fails closed to no access, not to admin.
- [The API server may not enforce CONNECT for this policy type] → D5 is gated on the live test, and D4 stands alone.
- [Root on the node can still read a running cell] → Stated plainly in the privacy statement. Key separation is the deferred remedy.

## Migration Plan

This lands after the first Cloud deploy's empty-data smoke test, and before any real vault import or friend admission.

1. Merge the chart change.
2. Run one fix-forward Helm upgrade of `exomem-platform`. Do not use `--atomic`: the legacy pause must never be rolled back.
3. Confirm the Certificate reaches Ready in `exomem-edge`, the public route serves the gateway, and Traefik's Role grants Secrets only in `exomem-edge`.
4. Issue the operator and break-glass certificates. Install their kubeconfigs root-only, and make the operator kubeconfig the default.
5. Confirm with the operator identity that reading a Secret and exec into a test cell are both denied.

Rollback: re-render with the previous chart values and fix forward. Never `helm rollback` past the release that recorded the legacy pause.
