## 1. Readiness state

- [x] 1.1 Add the `retrieval_proven` flag to `readiness.py`: set only while managed at `admit_retrieval_proof`, `mark_ready("retrieval_catalog")` and the re-admit branch of `retrieval_admission`; cleared in `manage_runtime`, `begin_warm`, `reset` and `unmanage_runtime`.
- [x] 1.2 Split the published-projection fallback out of `runtime_retrieval_catalog_admission` into a helper that schedules no repair, keep the request path's call shape, and add the probe's `served_catalog_lag`.
- [x] 1.3 Report `lagging` from `runtime_readiness` with `lagging_scopes` and `catalogue_unchanged_seconds`; accept it in `build_runtime_readiness` as ready, with `admitted` false and no reason code.
- [x] 1.4 Keep a `lagging` answer out of the ready-proof cache in `server_assets.py`.

## 2. Proof

- [x] 2.1 Test through `/health/ready` on a real vault: a proven process with an advanced live projection answers 200 with `lagging`, the lagging scopes and a numeric age.
- [x] 2.2 Test that a lost live projection, a missing published catalogue, a running warm, a warm that ends without a strict proof and an unmanaged proof each answer 503.
- [x] 2.3 Test that a lagging answer is not reused from the ready-proof cache, and that the age restarts only when the catalogue publishes again.
- [x] 2.4 Change `test_readiness_only_traffic_recovers_a_failed_catalogue_publication` to accept 200 with `lagging` mid-repair while it still waits for `retrieval.state == "ready"`.

## 3. Verification and delivery

- [x] 3.1 Run the scoped readiness, recovery, cloud-cell, service-manager and doctor tests and compare failure names with the `origin/main` baseline; run Ruff, the privacy gate and `openspec validate --all --strict`.
- [x] 3.2 Run the readiness repro on the branch and record the not-ready probe count after the first admission (an in-process adoption apply of 100 notes and 150 files of 300 KB: 0 of 44 samples not ready, 4 reported `lagging`).
