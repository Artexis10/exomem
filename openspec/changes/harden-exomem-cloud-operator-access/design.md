## Context

The platform chart runs Traefik as a subchart in the `exomem-platform` release namespace. Its ClusterRole reads Secrets in every namespace. The Cloud route (IngressRoute, Middleware, Issuer, Certificate, TLS Secret) lives in `exomem-cloud`, beside cellctl's and the gateway's keys: the cell token key, backup master key, B2 key-management credential and database DSNs. `exomem-platform` holds the Cloud volume-encryption passphrase and the legacy platform's Secrets. The operator uses K3s's admin kubeconfig (`system:masters`) for everything.

The node already runs a K3s audit policy (`infra/ansible/roles/k3s/files/audit-policy.yaml`). It records every request at Metadata level, and Secrets at Metadata only, retained 7 days on the node.

cellctl's own writes are already confined by ValidatingAdmissionPolicies. Tenant-to-tenant isolation is out of scope here; `cloud-cell` covers it.

## Goals / Non-Goals

**Goals:**
- A compromised edge proxy yields the public certificate and whatever live traffic passes while it is compromised: requests, responses and bearer tokens it could replay. It yields no durable key, meaning the cell token key, backup master key, B2 key-management credential or database DSNs. That turns a would-be fleet compromise into a live-session one. The gateway, which parses every public request, still holds `EXOMEM_CLOUD_CELL_TOKEN_KEY`; hardening the gateway is out of scope here.
- No operator command run through the everyday identity can reach vault content or tenant keys.
- Any deeper access is deliberate, uses a distinct identity, and is audited.

**Non-Goals:**
- Hiding content from root on a running node. That requires key separation or client-side encryption, which is deferred.
- Shipping audit logs off the node.
- Changing any cell-side feature.

## Decisions

### D1. Traefik moves to its own namespace inside the same release

Set the Traefik subchart's `namespaceOverride: exomem-edge`, `rbac.namespaced: true`, and `providers.kubernetesCRD.namespaces: [exomem-edge]`. Its Role then grants Secret reads only in `exomem-edge`. The chart renders no Traefik ClusterRole in this mode, because the template is gated on `not rbac.namespaced`. The platform chart creates the `exomem-edge` namespace with Pod Security `enforce: privileged` and `audit`/`warn: restricted`, matching `exomem-platform`. Restricted includes Baseline, which forbids any non-zero hostPort and has no allow-list, so enforcing it would refuse Traefik's hostPort 443 pod. Only the admin identity can create pods there. The namespace is default-deny: ingress only on Traefik's websecure port, egress only to the gateway, cluster DNS and the API server. A compromised edge process therefore cannot reach other in-cluster services, node ports or the cloud metadata endpoint.

- *Rejected: namespaced RBAC in place.* Traefik always watches its own release namespace, and `exomem-platform` holds the volume-encryption passphrase. The Cloud route namespace holds every Cloud key.
- *Rejected: a separate Helm release for Traefik.* It adds a second release lifecycle for no isolation gain over `namespaceOverride`.

### D2. The route reaches the gateway through an ExternalName Service

The IngressRoute and trusted-ingress Middleware move to `exomem-edge`. An ExternalName Service there points at `exomem-cloud-gateway.<cellctl.namespace>.svc.cluster.local`, with `providers.kubernetesCRD.allowExternalNameServices: true`. The gateway's NetworkPolicy admits ingress only from pods labelled `exomem.io/ingress: traefik` in the `exomem-edge` namespace.

- *Rejected: `allowCrossNamespace`.* Traefik would have to watch `exomem-cloud`, and namespaced RBAC would then grant it that namespace's Secrets.

### D3. DNS-01 runs through a ClusterIssuer whose credential stays in `exomem-cloud`

The Certificate moves to `exomem-edge`, and the TLS Secret it produces lands there. It names a ClusterIssuer. cert-manager's `clusterResourceNamespace` is set to `cellctl.namespace`, so the existing `exomem-cloudflare-dns-token` destination stays where the signed secret registry already places it. The edge never sees the DNS-edit token.

A ClusterIssuer can be named from any namespace, and cert-manager's edit ClusterRole aggregates into the built-in admin and edit roles. So a ValidatingAdmissionPolicy admits a Certificate or CertificateRequest naming this ClusterIssuer only in `exomem-edge`. That preserves the earlier review decision that only the Cloud route may obtain the MCP hostname's certificate.

- *Rejected: an Issuer in `exomem-edge`.* The token would sit beside Traefik, and a DNS-edit token is enough to hijack the hostname.
- *Rejected: moving the token's destination.* That changes the signed registry and needs a re-sign, for no gain.

### D4. Three identities, each with one purpose

1. **Everyday operator (`exomem-operator`).**
   - A client certificate issued through the Kubernetes CSR API (signer `kubernetes.io/kube-apiserver-client`), in group `exomem:operators`, with `expirationSeconds` of 30 days. The runbook re-issues it.
   - Bound to a ClusterRole with get/list/watch on pods, `pods/log`, events, namespaces, deployments, statefulsets, jobs, PVCs, services and nodes.
   - No Secrets and no connect subresources.
   - Its kubeconfig is the default in every runbook.
2. **Deploy (K3s admin kubeconfig, root-only).** Used by scripted Helm and apply procedures, which need broad write rights. The node originally wrote it `0640` to group `exomem-operators`, which holds the administrator login, so the K3s configuration now writes it `0600` root-owned. The administrator reaches it only through `sudo`, which is logged.
3. **Break-glass (`exomem-break-glass`).** Minted on demand through a CSR with `expirationSeconds: 3600`, in group `exomem:break-glass`, which is bound to `cluster-admin`. There is no standing file. The CSR's creation and approval are themselves audited. Client certificates cannot be revoked, so short expiry is the control.

- *Rejected: using the admin kubeconfig day to day.* Admin can read every Secret and exec anywhere, so a mistyped namespace becomes content access.

### D5. Admission denies connect subresources in cell namespaces to everyone but break-glass

A ValidatingAdmissionPolicy matches CONNECT on `pods/exec`, `pods/attach` and `pods/portforward`, and UPDATE on `pods/ephemeralcontainers`, in cell namespaces (`exo-cell-<id>`) and export scratch namespaces (`exo-scratch-<id>-<8 hex>`), which hold a restored plaintext vault. It denies these unless `request.userInfo.groups` contains `exomem:break-glass`. It reads only user info and the namespace. Admission applies to `system:masters` too, so the deploy identity cannot open a shell in a cell by accident. Masters can still delete the policy or approve their own break-glass CSR, so this guards against accidents, not intent. `pods/proxy` is denied to the everyday identity by RBAC (D4) and is not part of this policy.

The chart ships this only after the live-K3s suite proves the API server matches these CONNECT operations under this policy type. If it does not, the policy is left out, the identity split in D4 stands alone, and this design records the observed behaviour.

- *Rejected: an admission webhook.* It adds a service to run and fail closed, for the same effect.

Observed 2026-09-28 on K3s v1.35.6+k3s1: the API server enforces this policy on CONNECT and on the ephemeral-container UPDATE. As the admin (`system:masters`) in an `exo-cell-*` namespace, exec, attach, port-forward and `kubectl debug`, plus raw CONNECT requests on exec, attach and portforward, were all denied by `exomem-cell-connect-guard`. The same admin's exec outside cell namespaces, and the break-glass group's exec and debug, were admitted. The policy ships. Any procedure that execs into a cell therefore needs break-glass, deploy scripts included.

### D6. The export runbook refuses operator-held recipients

`docs/runbooks/cloud-operator-export.md` already streams the vault only through age to a recipients file. It gains two rules:
- the recipients file must be supplied by the tenant, with its fingerprint recorded before the restore;
- the procedure stops if any listed recipient matches a registered operator or escrow key.

Only age X25519 recipients are accepted, because SSH keys cannot be matched against the registered set. The tenant sends the file's SHA-256 separately; the operator compares it, and hashes the file again immediately before encrypting. The tenant's own verification counts the recipient stanzas in the archive header, which must equal the number of recipients the tenant supplied, and then decrypts with every one of the tenant's identities. The count catches an added key, and the decryptions catch a swapped one. Together they prove the archive is encrypted to exactly the tenant's recipients.

### D7. The audit policy already satisfies the break-glass record, on the node

Metadata-level logging captures the user, groups, verb, resource, subresource, namespace and time for every request, including exec and CSR approval, with no bodies. A task confirms this on the live node. The log lives on the node for 7 days and root can rewrite it, so it is a trail under the operator's control, not tamper-proof evidence. Shipping it off the node is deferred.

### D8. Content-free logs are proven with a canary

`pods/log` must be granted cluster-wide: RBAC cannot exclude cell namespaces created at runtime, and admission does not gate reads. So the guarantee rests on what cells and controllers emit. A live check writes a unique canary through a cell, exercises search, recall and review, then asserts the canary, and its base64 forms, appears in no pod log, in the cell, cellctl or the gateway. On the node it also searches the kubelet's log files, rotated ones included, because `kubectl logs` returns only the current file. It runs outside the nightly backup window, because a finished Job's logs disappear with its pod. Traefik access logs are off, so the edge is not scanned. It runs on the production node through the Cloud connector, as a runbook procedure, with a negative control that shows the same log search finds a deliberately echoed canary. The live-K3s suite cannot host it: its cell is a stand-in with no search, recall or review.

## Risks / Trade-offs

- [Moving Traefik changes its Service address, so the paused legacy `cloudflared` route stops resolving] → Accepted. Legacy is paused with zero cells and is being retired. The change notes it, and nothing Cloud-facing uses that route.
- [There is a brief 443 outage while Traefik moves and the Certificate re-issues in `exomem-edge`] → Run it before any real vault is imported. DNS-01 issuance takes minutes.
- [ExternalName routing hides a gateway outage behind DNS] → The gateway readiness probe and a post-deploy smoke check through the public route catch it.
- [The CSR-issued operator certificate expires] → The operator-access runbook records the expiry and re-issue steps. An expired certificate fails closed to no access, not to admin.
- [The API server may not enforce CONNECT for this policy type] → D5 is gated on the live test, and D4 stands alone.
- [Root on the node can still read a running cell] → Stated plainly in the privacy statement. Key separation is the deferred remedy.
- [A compromised edge sees live traffic and bearer tokens] → Accepted and disclosed. Isolation bounds it to live sessions, not durable keys. Token lifetime and refresh rotation limit how long a replayed token lasts.
- [Masters can remove the admission guards] → Accepted. D5 and the ClusterIssuer confinement stop accidents, and the audit trail records deliberate removal.

## Migration Plan

This lands after the first Cloud deploy's empty-data smoke test, and before any real vault import or friend admission.

1. Merge the chart change.
2. Run one fix-forward Helm upgrade of `exomem-platform`. Do not use `--atomic`: the legacy pause must never be rolled back.
3. Confirm the Certificate reaches Ready in `exomem-edge`, the public route serves the gateway, and Traefik's Role grants Secrets only in `exomem-edge`.
4. Issue the 30-day operator certificate. Install its kubeconfig root-only and make it the default. No break-glass credential is issued or stored.
5. Confirm with the operator identity that reading a Secret and exec into a test cell are both denied. Then mint a one-hour break-glass certificate, exec into the test cell, and confirm that the CSR approval and the exec appear in the audit log and that the certificate is rejected after expiry.

Rollback: re-render with the previous chart values and fix forward. Never `helm rollback` past the release that recorded the legacy pause.
