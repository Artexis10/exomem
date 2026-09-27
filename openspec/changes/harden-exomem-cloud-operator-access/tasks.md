## 1. Edge isolation (D1–D3)

- [ ] 1.1 Write red-first chart tests in `infra/cellctl/tests/test_platform_chart_headers.py` against `values.validation.yaml`:
  - Traefik's Deployment, Service and Role render in `exomem-edge`;
  - no Traefik ClusterRole grants Secrets;
  - the only Role granting Secrets to Traefik is in `exomem-edge`;
  - the IngressRoute, Middleware, Certificate and ExternalName Service render in `exomem-edge`;
  - the Certificate names a ClusterIssuer;
  - the gateway NetworkPolicy admits only `exomem.io/ingress: traefik` pods from namespace `exomem-edge`.
  Verify: the tests fail on current main.
- [ ] 1.2 Add the `exomem-edge` namespace to the platform chart, with Pod Security `enforce: privileged` and `audit`/`warn: restricted`, and a chart test pinning those labels. Set Traefik `namespaceOverride`, `rbac.namespaced`, `providers.kubernetesCRD.namespaces` and `allowExternalNameServices` in the Cloud values. Verify: 1.1's Traefik assertions pass, and `helm lint --strict` plus `infra/scripts/validate.sh` pass.
- [ ] 1.3 Move the IngressRoute and Middleware to `exomem-edge`. Add the ExternalName Service. Switch the Issuer to a ClusterIssuer, with cert-manager `clusterResourceNamespace` set to `cellctl.namespace`. Retarget the gateway NetworkPolicy. Verify: all 1.1 tests pass, and `test_platform_legacy_pause.py` and `tests/test_hosted_helm_contract.py` stay green.
- [ ] 1.5 Add a ValidatingAdmissionPolicy that admits a Certificate or CertificateRequest naming the Cloud ClusterIssuer only in `exomem-edge`. Verify: chart tests pin it, and the live-K3s suite shows such a Certificate refused in another namespace.
- [ ] 1.4 Extend the live-K3s suite (`infra/cellctl/tests/test_k3s_integration.py`) with an edge scenario:
  - Traefik's ServiceAccount is refused `get secret` in `exomem-cloud`, `exomem-platform` and a cell namespace;
  - a request through the public route reaches the gateway.
  Verify: the suite passes in the `cloud-cellctl` workflow's live job.

## 2. Operator identities and admission (D4–D5)

- [ ] 2.1 Write red-first chart tests for the `exomem-operator-read` ClusterRole and binding: no `secrets`, and no `pods/exec`, `pods/attach`, `pods/portforward`, `pods/proxy` or `pods/ephemeralcontainers`. Then add the template. Verify: the tests pass.
- [ ] 2.2 Add a live-K3s probe that applies the D5 ValidatingAdmissionPolicy and checks, as a non-break-glass user, whether exec, attach, port-forward and ephemeral containers in an `exo-cell-*` namespace are denied at admission. Record the observed result in this design. Verify: the probe runs in the live job and its outcome is written into D5.
- [ ] 2.3 If 2.2 proves enforcement, ship the policy and binding in the chart, with chart tests and a live test that the `exomem:break-glass` group is admitted. Otherwise record the decision in D5. Verify: the tests pass, or the D5 record is merged.
- [ ] 2.4 Write `docs/runbooks/cloud-operator-access.md`. It covers issuing the 30-day operator certificate through the CSR API, minting a one-hour break-glass certificate on demand (with no standing file), installing kubeconfigs root-only, re-issue, making the operator kubeconfig the default, and finding CSR approvals and break-glass use in `/var/lib/rancher/k3s/server/logs/audit.log`. Verify: the privacy gate passes, and the runbook commands pass the live-K3s rehearsal.
- [ ] 2.5 Add the D8 canary test to the live-K3s suite: seed a unique string through a cell, exercise search, recall and review, and assert it appears in no cell, cellctl or gateway log. Verify: it passes in the live job and fails when a deliberate log line echoes the canary.

## 3. Tenant-only exports (D6)

- [ ] 3.1 Update `docs/runbooks/cloud-operator-export.md`:
  - the recipients file comes from the tenant, and its fingerprint is recorded before the restore;
  - the procedure stops when any recipient matches a registered operator or escrow key.
  Verify: a reviewer walks the edited procedure against the spec's "Operator supplies their own recipient" scenario.

## 4. Deployment and closure

- [ ] 4.1 After the Cloud smoke test, apply the chart through one fix-forward upgrade of `exomem-platform` (no `--atomic`). Verify:
  - the Certificate is Ready in `exomem-edge`;
  - the public route serves the gateway;
  - `kubectl auth can-i get secrets -n exomem-cloud --as=system:serviceaccount:exomem-edge:<traefik>` is `no`.
- [ ] 4.2 Issue and install the operator and break-glass identities on the node. Verify with the operator identity that reading a Secret and exec into a test cell are both denied, and that a break-glass exec appears in the audit log without content.
- [ ] 4.3 Review user-facing Cloud privacy copy against the spec's privacy requirement. Verify: every claim maps to a stated property, and both disclosures (content in transit at the edge and gateway, and operator-held keys) are present.
