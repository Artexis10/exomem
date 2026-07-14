# Hosted Runtime Verification

## Real image drill (2026-07-14)

Built the repository-owned hosted target with an immutable canonical release time:

```text
docker build --target hosted \
  --build-arg EXOMEM_RELEASE_BUILD_TIME=2026-07-14T03:10:33Z \
  -t exomem:hosted-runtime-contract .
image: sha256:3ad8795da55a3db3fa86a08127b624309a13972996a38580293fe9cad9852d93
```

The fixed-identity/read-only smoke check proved `10001:10001`, no declared
Docker volume, a non-writable image root, preserved release metadata, and the
JSON hosted operator entry point.

The reproducible real-image drill was then run with:

```text
.venv/bin/python scripts/verify-hosted-image.py \
  --image exomem:hosted-runtime-contract
```

Observed result:

```json
{"aborted_replay_code":"TRANSFER_GRANT_REJECTED","chunked_status":201,"init":"HOSTED_CELL_INITIALIZED","large_peak_temp_bytes":94371840,"large_status":201,"read_only_root":true,"restart_temp_counts":"0 0","v1_partial_multipart_bytes":3146075,"v1_restart_runtime_temp_entries":0,"v1_upload_status":201}
```

The drill additionally verified the committed `large-90m.bin` and
`chunked-5m.bin` byte counts and SHA-256 digests from inside the mounted vault.
The interrupted v2 grant was consumed before use and required a new grant on
retry. A kill during an admitted v2 upload left a recognized temp file; restart
cleared both the 96 MiB v2 root and an independently injected diarizer runtime
artifact before a fresh chunked upload succeeded. A separate legacy-bound cell
was killed during a 3 MiB private-v1 multipart request; restart cleared the
shared 16 MiB runtime root, and a fresh authenticated v1 multipart upload then
succeeded inside its immutable build-relative deadline.

## Repository gates (2026-07-14)

```text
focused hosted/container tests: 306 passed, 2 skipped
complete lean suite:            2572 passed, 21 skipped
privileged restore/device run:  5 passed
latency gate:                   2 passed
installed-wheel product E2E:    PASS (17.8s)
scoped Ruff correctness:        passed
wheel/sdist build:              exomem 0.22.0 passed
strict OpenSpec validation:     passed
hosted Docker build/drill:      passed
```

The two focused hosted skips were separately covered by the privileged root
container run. The 21 lean-suite skips are the intentional optional model,
media, platform, and privileged cases reported by pytest. Repository-wide Ruff
still reports the pre-existing unscoped backlog; every changed Python file
passes the configured correctness rules, and both new Python files pass Ruff
format checking.

## Cross-repository release proof (2026-07-14)

The companion infrastructure branch pins this exact runtime commit
`c255ffb2dfcd7bc470372d4efa0e8a11b00f0640`, release `0.22.0`, hosted protocol
`1`, and operator-contract SHA-256
`407799e723e9d996e5ab15ca76c071c3ae497041a1096f106690712ce6fe4ca6`.
Its exact K3s `v1.35.6+k3s1` gate builds the hosted target, imports only the
computed image digest, and proves a real PVC/subPath mount, unchanged
`0700` roots and `0600` markers, native root-owned `0444` Secret AtomicWriter
projection, read-only-root UID/GID 10001 serving, state-backed temporary roots,
authenticated readiness/contract, and restart cleanup. The final admission
review additionally proved that the exact Job-controller finalizer exception
requires an unchanged Pod spec; routine removal and simultaneous
`activeDeadlineSeconds` or toleration drift are denied. The independently
reviewed gate passed 31 tests plus Terraform, Ansible, Helm, Kubeconform, Ruff,
ShellCheck, strict OpenSpec, and the real runtime drill.

Substrate PR #32 head `6d48e023f3c5a4780c212cef34fc784bf3b1f068`
contains the generated semantic fixture from this exact commit. It pins release
`0.22.0`, protocol `1`, and gateway-contract digest
`49ac4d346991f0f1de5f692a78ad043de6020f9a1692cafc951ec84490f02940`;
its parser rejects semantic or digest drift and its direct-transfer issuance
and browser consumption tests use the matching v2 artifact.

Independent runtime, cross-repository contract, transfer, and admission reviews
resolved every blocking finding. PR #227 is titled
`feat: complete isolated hosted tenant runtime`, records the exact release unit,
and has green Python 3.11/3.13, product E2E, retrieval, onboarding, Docker,
OpenSpec, package, capability, and lint/type checks.
