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

## Still external or pending

- Task 5.1 remains open because the real K3s manifests/PVC/AtomicWriter checks
  belong to the infrastructure repository and have not been executed here.
- Task 5.3 remains open pending the coordinated downstream Substrate fixture
  refresh.
- Task 5.5 remains open until the independent implementation review and PR
  metadata refresh are complete.
