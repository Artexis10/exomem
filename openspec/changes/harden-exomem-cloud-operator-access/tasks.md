## 1. Edge isolation (D1–D3)

- [x] 1.1 Write red-first chart tests in `infra/cellctl/tests/test_platform_chart_headers.py` against `values.validation.yaml`:
  - Traefik's Deployment, Service and Role render in `exomem-edge`;
  - no Traefik ClusterRole grants Secrets;
  - the only Role granting Secrets to Traefik is in `exomem-edge`;
  - the IngressRoute, Middleware, Certificate and ExternalName Service render in `exomem-edge`;
  - the Certificate names a ClusterIssuer;
  - the gateway NetworkPolicy admits only `exomem.io/ingress: traefik` pods from namespace `exomem-edge`.
  Verify: the tests fail on current main.
  Evidence: `infra/cellctl/tests/test_platform_chart_headers.py` edge tests; 9 failed on the unchanged chart, then passed.
- [x] 1.2 Add the `exomem-edge` namespace to the platform chart, with Pod Security `enforce: privileged` and `audit`/`warn: restricted`, and a chart test pinning those labels. Set Traefik `namespaceOverride`, `rbac.namespaced`, `providers.kubernetesCRD.namespaces` and `allowExternalNameServices` in the Cloud values. Verify: 1.1's Traefik assertions pass, and `helm lint --strict` plus `infra/scripts/validate.sh` pass.
  Evidence: the `exomem-edge` namespace-label chart test; `helm lint --strict` clean.
- [x] 1.3 Move the IngressRoute and Middleware to `exomem-edge`. Add the ExternalName Service. Switch the Issuer to a ClusterIssuer, with cert-manager `clusterResourceNamespace` set to `cellctl.namespace`. Retarget the gateway NetworkPolicy. Verify: all 1.1 tests pass, and `test_platform_legacy_pause.py` and `tests/test_hosted_helm_contract.py` stay green.
  Evidence: all 1.1 tests pass; `test_platform_legacy_pause.py` (14) and `tests/test_hosted_helm_contract.py` green.
- [x] 1.4 Extend the live-K3s suite (`infra/cellctl/tests/test_k3s_integration.py`) with an edge scenario:
  - Traefik's ServiceAccount is refused `get secret` in `exomem-cloud`, `exomem-platform` and a cell namespace;
  - a request through the public route reaches the gateway.
  Verify: the suite passes in the `cloud-cellctl` workflow's live job.
  Evidence: `test_k3s_integration.py` edge scenario (Traefik can-i `no` in three namespaces; `/mcp` 200 with the trusted header overwritten); full live module 5 passed.
- [x] 1.5 Add a ValidatingAdmissionPolicy that admits a Certificate or CertificateRequest naming the Cloud ClusterIssuer only in `exomem-edge`. Verify: chart tests pin it, and the live-K3s suite shows such a Certificate refused in another namespace.
  Evidence: chart test pins `exomem-cloud-issuer-scope`; live test shows a Certificate naming the ClusterIssuer refused outside `exomem-edge`.
- [x] 1.6 Make `exomem-edge` default-deny (D1): ingress only on websecure, egress only to the gateway, cluster DNS and the API server. Verify: chart tests pin the policy, and the live-K3s edge scenario still serves `/mcp` through Traefik with the policy applied, while an egress probe from the edge to another namespace is refused.
  Evidence: `default-deny` and `traefik` NetworkPolicies pinned by chart tests; live, `/mcp` 200 and Traefik Ready under the policy, while `exomem-platform` and kubelet 10250 are refused. API egress is the node's `/32` on 6443 (`edge.apiServerCidrs`), because kube-router matches after DNAT.

## 2. Operator identities and admission (D4–D5)

- [x] 2.1 Write red-first chart tests for the `exomem-operator-read` ClusterRole and binding: no `secrets`, and no `pods/exec`, `pods/attach`, `pods/portforward`, `pods/proxy` or `pods/ephemeralcontainers`. Then add the template. Verify: the tests pass.
  Evidence: chart tests for `exomem-operator-read` (red, then green); independent review found no `nodes/proxy`, CSR, impersonate or wildcard grants.
- [x] 2.2 Add a live-K3s probe that applies the D5 ValidatingAdmissionPolicy and checks, as a non-break-glass user, whether exec, attach, port-forward and ephemeral containers in an `exo-cell-*` namespace are denied at admission. Record the observed result in this design. Verify: the probe runs in the live job and its outcome is written into D5.
  Evidence: live probe on K3s v1.35.6; the result is recorded in D5.
- [x] 2.3 If 2.2 proves enforcement, ship the policy and binding in the chart, with chart tests and a live test that the `exomem:break-glass` group is admitted. Otherwise record the decision in D5. Verify: the tests pass, or the D5 record is merged.
  Evidence: `exomem-cell-connect-guard` shipped with chart tests; live, the admin is denied in cell and scratch namespaces and the `exomem:break-glass` group is admitted.
- [x] 2.4 Write `docs/runbooks/cloud-operator-access.md`. It covers issuing the 30-day operator certificate through the CSR API, minting a one-hour break-glass certificate on demand (with no standing file), installing kubeconfigs root-only, re-issue, making the operator kubeconfig the default, and finding CSR approvals and break-glass use in `/var/lib/rancher/k3s/server/logs/audit.log`. Verify: the privacy gate passes, and the runbook commands pass the live-K3s rehearsal.
  Evidence: `docs/runbooks/cloud-operator-access.md`; its marked blocks pass the live-K3s rehearsal, and the privacy gate is clean.
- [x] 2.5 Write the D8 canary procedure into `docs/runbooks/cloud-operator-access.md`: seed a unique string through a cell over the Cloud connector, exercise search, recall and review, then search every cell, cellctl and gateway pod log with the operator identity. Include a negative control that echoes the canary from a throwaway pod outside cell namespaces and shows the same search finds it. Verify: the privacy gate and shellcheck pass on the runbook blocks; the live run is task 4.4.
  Evidence: the "Proving logs are content-free" section; it was rehearsed against fake kubectl and kubelet files (clean, gateway-previous hit, gzipped base64 hit and partial-record split cases), and shellcheck is clean.
- [x] 2.6 Make the K3s admin kubeconfig root-only (D4): the K3s configuration template writes it `0600`, root-owned, with no group. Verify: a red-first Ansible contract test pins the mode and the absence of `write-kubeconfig-group`.
  Evidence: the `config.yaml.j2` mode `0600` with no group, plus a `file:` task enforcing `root:root 0600`; the Ansible contract tests were red, then green.
- [x] 2.7 Make the admission policies' logic, not just their text, a pull-request gate: run the live-K3s job on pull requests that touch any platform template, the values schema or validation values, the chart's `Chart.yaml`/`Chart.lock`, the cell manifest renderer or the live test itself, failing open when the diff cannot be computed. Verify: tests that run the scope step against real repositories cover a rename, a 3,000-file pull request and an uncomputable diff, and a mutation that inverts a policy's rule fails the live test.
  Evidence: `.github/workflows/cloud-cellctl.yml` `live-k3s-scope`; `infra/cellctl/tests/test_live_k3s_gate.py` (15 passed, with the rename and large-diff cases red on the first version); inverting the guard left 47 chart tests green while the live connect test failed.

## 3. Tenant-only exports (D6)

- [x] 3.1 Update `docs/runbooks/cloud-operator-export.md`:
  - the recipients file comes from the tenant, and its fingerprint is recorded before the restore;
  - the procedure stops when any recipient matches a registered operator or escrow key.
  Verify: a reviewer walks the edited procedure against the spec's "Operator supplies their own recipient" scenario.
  Evidence: `docs/runbooks/cloud-operator-export.md` accepts only age recipients matching a tenant-sent SHA-256, stops on registered operator or escrow keys, re-hashes before encrypting, and has the tenant count stanzas and decrypt with every identity; it was rehearsed with age 1.2.1 (added-key and swapped-key archives refused), and independent review walked it.

## 4. Deployment and closure

- [ ] 4.1 After the Cloud smoke test, apply the chart through one fix-forward upgrade of `exomem-platform` (no `--atomic`). Verify:
  - the Certificate is Ready in `exomem-edge`;
  - the public route serves the gateway;
  - `kubectl auth can-i get secrets -n exomem-cloud --as=system:serviceaccount:exomem-edge:<traefik>` is `no`;
  - after a Traefik restart, which forces a fresh API watch under the edge policy, the public route still serves the gateway. A wrong `edge.apiServerCidrs` otherwise fails only at the next restart.
- [ ] 4.2 Issue and install only the 30-day operator identity on the node. Verify:
  - with the operator identity, reading a Secret and exec into a test cell are both denied;
  - a freshly minted one-hour break-glass certificate can exec into the test cell;
  - the CSR approval and the exec appear in the audit log without content;
  - that certificate is rejected after expiry.
- [ ] 4.3 Review user-facing Cloud privacy copy against the spec's privacy requirement. Verify: every claim maps to a stated property, and both disclosures (content in transit at the edge and gateway, and operator-held keys) are present.
- [ ] 4.4 Run the D8 canary procedure on the production node after 4.1. Verify: the canary appears in no cell, cellctl or gateway log, and the negative control finds it.
- [ ] 4.5 Apply 2.6 on the node: set the admin kubeconfig to `0600 root:root` and deploy the K3s configuration change. Verify: `stat` shows `0600 root root`, and the administrator login cannot read it without `sudo`.
