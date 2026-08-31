## 1. Pin the Active-Compute Failure Contracts

- [x] 1.1 Add red pure-logic tests for `EXOMEM_CPU_THREADS` and `EXOMEM_SYNC_WORKERS` defaults, valid overrides, rejection below two sync workers, derived `min(4, floor(sync/2))` model admission across 2/3/4/8/16 workers, the explicit unsafe native-override escape hatch, and allocation-free status collection; verify the focused tests fail for the missing policy.
- [x] 1.2 Add red subprocess tests proving both product entrypoints replace inherited BLAS/OpenMP/Rayon/tokenizer values before importing NumPy or model modules; verify an inherited value of 32 survives today and the current oversized thread posture is reproducible.
- [x] 1.3 Add red tests for a common local/hosted AnyIO lifespan, eight default general workers, derived model admission that always reserves at least half/one general worker, reentrant one-at-a-time model execution, and retryable rejection beyond that queue; test the default four-model-call case plus smaller override matrices while a quick sync status call completes within the latency ceiling.
- [x] 1.4 Add red adapter tests for explicit CTranslate2, ONNX Runtime, and PyTorch thread arguments, media-child background priority, and systemd quota rendering at 1/2/8/32 online CPUs; verify each fails against the current unbounded wiring.

## 2. Implement the Portable Compute Envelope

- [x] 2.1 Add the dependency-light runtime resource policy, bootstrap it from the server/CLI and media-child entrypoints, and verify the policy and early-import subprocess tests pass.
- [x] 2.2 Apply the authoritative budget explicitly to CTranslate2, ONNX Runtime, PyTorch, indirect BLAS/OpenMP/Rayon/tokenizer users, and the common AnyIO lifespan; verify larger inherited library values are replaced unless the unsafe escape is explicit and optional model stacks stay absent from status paths.
- [x] 2.3 Add bounded reentrant model admission and one-at-a-time execution for embedding/reranker/CLIP/ASR seams while retaining general sync-tool capacity; verify four held model callers cannot overlap or block a quick status call and a fifth model caller fails retryably without consuming another worker.
- [x] 2.4 Lower disposable media-child scheduling priority and render `CPUWeight=20` plus `CPUQuota=min(400, 50 * online-logical-cpus)%` into the Linux user service; verify POSIX/Windows priority fakes, 50/100/400/400 quota cases, and service-installer tests pass.
- [x] 2.5 Expose the effective native-thread budget, sync-worker budget, model admission/execution posture, override sources, unsafe escape, and scheduling posture through resource status and doctor; verify no-allocation/instant-start tests prove no model import, accelerator initialization, or worker launch.

## 3. Pin the Accelerator and Failure-State Contracts

- [x] 3.1 Add red selection tests for the device-preference × computation-override matrix across quiet/normal/performance workers using an ASR-specific CTranslate2/headroom probe, CUDA `float16` safety computation, CPU `int8`, and concrete overrides; cover explicit CPU/CUDA, automatic CUDA decline, `auto`/unknown/unsupported override refusal, CTranslate2 healthy with torch absent, torch healthy with CTranslate2 broken, and CTranslate2 4.8 falsely reporting `sm_120` INT8.
- [x] 3.2 Add red installation/subprocess tests for `ctranslate2>=4.6.3,<5`, `nvidia-cublas-cu12>=12.8.4.1,<13`, `nvidia-cuda-runtime-cu12>=12.8.90,<13`, and `nvidia-cudnn-cu12>=9.5.0.50,<10` on Linux/Windows, parent-supplied child loader paths, an incompatible system `libcublas.so.12` earlier in the parent environment, and selected native components below each floor; verify torch CUDA and path precedence alone cannot satisfy ASR readiness.
- [x] 3.3 Add red lazy-execution, fault-point, and upgrade tests for CUDA/cuBLAS/cuDNN/driver failures during model construction and segment consumption; verify the current generic state, sidecar-first crash window, retained legacy `FAILED` row/sidecar, repeated queued model attempt, and artifact-repair advice violate the blocked contract.

## 4. Repair ASR Accelerator Routing and Runtime Ownership

- [x] 4.1 Add ASR-specific CTranslate2/headroom admission and permit its healthy CUDA result in normal/performance disposable workers while retaining quiet-mode and absent/declined-accelerator bounded CPU behavior; verify the torch-independent mode/device matrix passes.
- [x] 4.2 Replace forced CUDA `int8_float16` with the device/override matrix's CTranslate2-supported `float16` safety default, retain CPU `int8`, add the disclosed concrete computation-type override, and verify an `sm_120` capability fake that advertises INT8, plus `auto`/unknown/unsupported values, never selects the broken quantized path.
- [x] 4.3 Package the pinned CUDA 12 cuBLAS/cuDNN/runtime floors plus direct CTranslate2 bounds on Linux/Windows, construct the wheel-owned runtime environment in the parent, and pass it to media-child `Popen`; make the verifier launch the same preconfigured hidden probe subprocess, report/reject selected native component versions, regenerate the lockfile, and verify metadata floors, shadow-library tests, and `uv lock --check` pass.
- [x] 4.4 Introduce a typed compute-runtime extraction failure and a ledger-first crash-consistent blocked protocol; verify fault after ledger marking converges the sidecar without ASR, the source artifact is unchanged, explicit retry reuses the job, and status never recommends artifact repair.
- [x] 4.5 Add bounded startup reconciliation for retained legacy accelerator failures and a worker-lifetime circuit breaker for subsequent queued ASR jobs; verify old failed rows/sidecars converge to blocked and one runtime failure prevents repeated model construction.
- [x] 4.6 Update the explicit media GPU verifier to use product routing and perform actual tiny-model compute in its preconfigured child before success; verify CPU-only reporting stays soft and mocked CUDA enumeration without model execution cannot pass.
- [ ] 4.7 Mark machine-owned sidecar boundaries explicitly; block ambiguous unmarked nonempty legacy notes before metadata or queue mutation; collapse repeated residue only against a surviving or freshly source-derived extraction candidate, never by inferring a blank-top period; verify two copies, differing prose, document-heading collisions, frontmatter, safety, idempotence, and repeated updates.

## 5. Verify Active Host Cooperation

- [ ] 5.1 Extend the resource-envelope verifier with deterministic process-tree metric tests and a real Linux active-cgroup mode that starts a transient unit containing a sample-vault Exomem server plus a non-cooperative child; verify aggregate five-second CPU stays within quota plus 0.25 core, every health/status probe stays below one second, thread count is diagnostic, and unsupported systemd is not reported as a pass.
- [x] 5.2 Run the focused CPU-envelope, media-worker, extraction, status/doctor, installer, and verification-script suites plus Ruff; record exact scoped commands and results in the lane result.
- [x] 5.3 Have an author-independent reviewer inspect and reproduce the CPU-envelope diff, return findings to the implementation lane, and verify every accepted finding is fixed before accelerator work is accepted.
- [x] 5.4 Have a fresh author-independent reviewer inspect and reproduce the accelerator/runtime/failure-state diff, return findings to the implementation lane, and verify every accepted finding is fixed.
- [ ] 5.5 Have an author-independent reviewer reproduce the exact-duplicate sidecar regression and verify the repair cannot discard non-whitespace authored residue before accepting it.

## 6. Completion and Release Evidence

- [ ] 6.1 Run `openspec validate --all --strict`, focused changed-behavior tests, lock/install checks, and Ruff locally; use the pull request's required CI for the full lean corpus and verify it is green or document only demonstrably unrelated baseline red with failure-name evidence.
- [ ] 6.2 Run the active systemd cgroup probe and real tiny-model ASR child probe in the target service interpreter where host access permits; record selected device/type/runtime, aggregate process-tree CPU, diagnostic threads, health/status latency, and any explicit environment limitation without treating enumeration as GPU proof.
- [ ] 6.3 Reconcile the implemented behavior with every OpenSpec task and delta scenario, check only evidence-backed boxes, and leave the change ready for post-merge spec sync/archive rather than claiming unshipped closure.
- [ ] 6.4 Commit only the intended scope, integrate current `origin/main` safely in the isolated task workspace, push the branch, and open a ready Conventional Commit pull request containing rationale and verification evidence; keep the workstation containment drop-ins until the released build is deployed and measured.
