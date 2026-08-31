## Context

See [proposal.md](proposal.md) for the incident and motivation. The existing disposable-worker design limits worker count, model residency, and idle lifetime, but active native compute is outside that envelope. One Python process can initialize independent OpenMP/BLAS, CTranslate2, ONNX Runtime, PyTorch, and tokenizer pools; their defaults are not coordinated and the native service entrypoints currently establish no process-wide budget before importing them. A staged reproduction in the installed service interpreter grew from 16 threads after importing the media worker, to 32 after loading BGE, to 56 after one encode. Adding the framework-default 40 AnyIO synchronous workers reproduced the long-lived service's observed 97-thread shape. The media queue itself is serialized; stacked native runtimes plus unconstrained request admission are the multiplier.

The ASR path has a second, independent defect. It forces CTranslate2 `int8_float16` whenever CUDA is selected. CTranslate2 4.8.0 incorrectly reports INT8 as supported on `sm_120`, and its `auto` resolver therefore selects the same `int8_float16` path; the upstream `sm_120` exclusion present in 4.6.3 is absent in 4.8.0 and 4.8.1. The affected interpreter also resolves CTranslate2's `libcublas.so.12` to a system CUDA 12.0 build while a compatible 12.8 build exists outside its loader path. PyTorch accelerator readiness is not evidence for faster-whisper because CTranslate2 loads its own CUDA 12 runtime. There is no product CPU fallback after this failure today: the failed job remains failed, while its failure-sidecar graph/embedding fanout and another normal-mode cell can simultaneously consume CPU.

The repair must work for direct CLI launches, Windows services, Linux systemd user services, macOS launchd services, and CPU-only hosts. Resource diagnostics must remain allocation-free. The existing drop-in quotas on the affected workstation are operational containment and remain until the released behavior is deployed and measured.

## Goals / Non-Goals

**Goals:**

- Establish one portable compute-envelope authority that can be applied before native runtimes initialize and queried without importing them.
- Bound each process independently so multiple cells compose predictably, then add service-manager containment for dependencies that ignore the in-process contract.
- Make the disposable worker's accelerator choice and computation type match the runtime it actually uses.
- Preserve retryable durable work while distinguishing compute infrastructure failures from bad artifacts.
- Verify both the declarative configuration and real model execution; enumeration alone is not accelerator proof.

**Non-Goals:**

- Dynamically maximize throughput from host load average, pressure-stall information, or cgroup feedback. Those signals are platform-specific and reactive; they are not the safety boundary.
- Add an automatic CUDA-to-CPU retry after execution has begun. An unnoticed fallback is precisely the failure mode this change must prevent.
- Replace faster-whisper/CTranslate2, redesign the media queue, or make media extraction a long-lived in-process service.
- Guarantee a particular transcription throughput on every CPU or GPU generation.

## Decisions

### 1. A bootstrap-safe module owns the portable CPU envelope

Add a dependency-light runtime resource module that parses `EXOMEM_CPU_THREADS` and `EXOMEM_SYNC_WORKERS`, exposes their values and sources, installs native thread environment values, applies framework-specific limits when those frameworks are intentionally loaded, configures the server's AnyIO limiter, and reports unsafe compatibility escapes.

The default is one native compute thread per Exomem process and eight concurrent synchronous requests per long-lived server. `EXOMEM_SYNC_WORKERS` accepts integers of at least two. Model admission is derived as `min(4, floor(sync_workers / 2))`: no more than four request threads may be active or waiting for the one-at-a-time model execution gate, and at least half of the effective pool always remains general capacity. The next contender receives a retryable compute-busy result immediately. At the default, four model callers leave four workers available for quick health, status, and filesystem/database work; at the minimum, one model caller leaves one. The gate is explicitly reentrant for the owning thread so nested model seams cannot deadlock; initialization locks remain separate. This is deliberately conservative: a service parent and its disposable child can make progress concurrently, excess model clients cannot fill the request pool, and an operator can raise the general budgets explicitly on a dedicated server. Invalid values fail clearly instead of reverting to an unsafe runtime default.

The module writes OpenMP, MKL, OpenBLAS, BLIS, NumExpr, Rayon, and tokenizer settings to the common budget before imports, replacing inherited larger values. `EXOMEM_ALLOW_NATIVE_THREAD_OVERRIDES=1` is the only escape hatch that preserves library-specific values, and diagnostics mark that process unsafe. It passes the common value directly to APIs whose defaults are otherwise ambiguous: CTranslate2 receives `cpu_threads=<budget>` and `num_workers=1`; ONNX Runtime receives `intra_op_num_threads=<budget>` and `inter_op_num_threads=1`; PyTorch receives intra-op and inter-op limits before first parallel work. A common FastMCP lifespan wrapper installs the AnyIO limit for local and hosted construction, so streamable HTTP, REST, and stdio share one admission contract.

Both `python -m exomem` and `python -m exomem.media_worker_child` call the bootstrap before importing heavy application/model modules. Package import does not mutate the host process, so embedding Exomem as a library remains unsurprising.

Alternatives considered: deriving a native pool size from `os.cpu_count()` repeats the incident on large desktops; load/PSI feedback reacts after contention and behaves differently by platform; reducing AnyIO itself to one head-of-line blocks quick non-model tools; a fixed model-admission value can consume every token after a smaller sync-worker override; a blocking model lock with no admission limit lets model waiters occupy every AnyIO token; a non-reentrant gate deadlocks nested seams; merely disclosing inherited library values preserves the Windows/direct-launch incident; setting only `OMP_NUM_THREADS` misses ONNX and framework-owned pools; setting only API knobs misses transitive BLAS users.

### 2. Background priority and a finite Linux cgroup ceiling backstop the runtime budget

The media child lowers its own scheduling priority after acquiring its single-worker vault claim and before processing work: POSIX uses niceness and Windows uses the below-normal process priority class. Failure to lower priority is reported but does not corrupt or abandon a job. macOS launchd retains its existing background service posture.

The Linux installer renders `CPUWeight=20` and `CPUQuota=<min(400, 50 * online-logical-cpus)>%`. Online CPUs come from the process's effective online/cpuset view with a fail-safe value of one. The result reserves at least half the machine, gives a one-CPU host a 50% ceiling, and never grants one cell more than four cores. The quota covers the service cgroup, including the disposable child. The in-process budget remains the cross-platform primary control and the quota is the last-resort ceiling when a native dependency ignores it. Operators can replace either value with a deliberate systemd drop-in.

Alternatives considered: `CPUWeight` alone yields only when another runnable cgroup competes and therefore is not a ceiling; a quota alone protects the host but gives interactive work no preference below the ceiling; shipping only machine-local drop-ins leaves direct launches and other platforms exposed.

### 3. Disposable ASR uses the accelerator in normal and performance modes, but never hides a runtime failure

Quiet mode remains CPU-only unless the operator explicitly requests another device. Normal and performance disposable workers may select CUDA when an ASR-specific admission probe succeeds because their model context is released with the child process. That probe combines allocation-free NVIDIA headroom with CTranslate2's own CUDA device count and supported computation types; it does not import or consult torch. A working CTranslate2 install without torch is admissible, while working torch with broken CTranslate2 is not. CPU-only and preflight-declined devices use the bounded CPU path; an explicitly requested CUDA device is refused when the ASR probe fails.

CUDA ASR defaults to `float16`; CPU remains `int8`. `EXOMEM_ASR_COMPUTE_TYPE` provides an explicit disclosed concrete-type override. The override value `auto` and unknown values are rejected: omission selects Exomem's safe default, while `auto` would delegate the decision back to the policy that caused the incident. A concrete override must be reported supported on the selected device. On compute capability 12.x, Exomem rejects every INT8-family override even when CTranslate2 4.8 reports it, because that report is the known false positive; other concrete reported types remain operator-selectable. CUDA admission requires CTranslate2 to report `float16` support, but capability reporting is only a preflight: a tiny real model-execution probe remains the release/readiness proof.

Device and computation precedence is explicit. An explicit CPU device uses bounded CPU and the CPU default or a CPU-supported concrete override. An explicit CUDA device requires CUDA admission and a CUDA-supported safe computation type; any failure is a typed refusal with no CPU fallback. Automatic normal/performance routing uses CUDA `float16` when admitted, otherwise bounded CPU `int8`; when a concrete computation override is present, automatic fallback may use CPU only if that exact type is reported supported on CPU, otherwise it refuses rather than silently ignoring the override. Quiet mode follows the CPU row unless the operator explicitly requests CUDA, in which case the explicit-CUDA row applies.

Device selection before model construction may choose CPU when CUDA is absent or declined. Once CUDA model construction or execution begins, a CUDA/cuBLAS/cuDNN/driver failure is fatal for that attempt and is not retried silently on CPU. The worker circuit-breaks the same ASR runtime for the rest of its lifetime: subsequent queued audio/video jobs are blocked with the same remediation without constructing the model again. The operator repairs the runtime or explicitly selects bounded CPU, then retries the durable jobs.

Alternatives considered: CTranslate2 `auto` appears portable but 4.8 resolves it to the incident's broken INT8 path on `sm_120`; preserving `int8_float16` repeats the failure directly; automatic CPU fallback obscures the fault and can recreate host saturation. A CUDA device that does not report `float16` support is declined to bounded CPU unless the operator explicitly selects another reported type.

### 4. The media extra owns the CUDA 12 runtime CTranslate2 consumes

Linux and Windows media installs include CUDA 12 wheels with enforceable floors: `nvidia-cublas-cu12>=12.8.4.1,<13`, `nvidia-cuda-runtime-cu12>=12.8.90,<13`, and `nvidia-cudnn-cu12>=9.5.0.50,<10`. The media extra directly requires `ctranslate2>=4.6.3,<5`, whose 4.6.3 release added CUDA 12.8 support, instead of relying on faster-whisper's transitive lower bound. That range is not itself an INT8-safety guarantee: later 4.8 releases regressed the `sm_120` exclusion, which is why Exomem owns the computation matrix above. A dependency-light helper discovers the installed `nvidia/*/lib` or `nvidia/*/bin` directories in dependency order and constructs the child environment. The media parent passes that environment to `Popen`; on Linux `LD_LIBRARY_PATH` is therefore correct before the child interpreter and dynamic loader start, while Windows also receives the wheel directories on `PATH` before CTranslate2's first library load. Windows may additionally register DLL directories after startup, but no Linux correctness claim depends on mutating `LD_LIBRARY_PATH` in-process.

The standalone verifier is a parent/child command: the parent constructs the same runtime environment and launches a hidden probe subprocess, and only that child imports CTranslate2/faster-whisper. The probe reports the selected wheel metadata and resolved native cuBLAS, CUDA-runtime, and cuDNN identities/versions, and refuses any selected component below the floors above. Installer/loader tests place an incompatible fake system `libcublas.so.12` earlier in the parent's environment and prove that a fresh child resolves the wheel-owned path first; path precedence alone is not accepted as version proof.

The explicit GPU verifier uses the product's device and computation policy, performs a tiny real transcription/encoder operation, and reports success only after model compute. It reports the CTranslate2 and CUDA runtime identities it proved. A torch probe can be reported separately but cannot satisfy ASR readiness.

Alternatives considered: relying on a system CUDA toolkit makes otherwise isolated installs depend on mutable host state; relying on PyTorch fails when its CUDA major differs; device enumeration proves neither native library resolution nor GEMM support.

### 5. Compute-runtime failures are a first-class blocked state

Introduce a typed media compute-runtime error at the extraction boundary. It wraps recognized CUDA, cuBLAS, cuDNN, driver, and native initialization/execution failures both during model creation and lazy segment consumption, preserving a concise cause without classifying ordinary decode/file errors as infrastructure.

There is no atomic transaction spanning the SQLite job ledger and Markdown sidecar. For typed compute failures the worker therefore uses a ledger-first crash protocol: mark the durable job `blocked` with structured runtime remediation, then publish/converge the canonical sidecar under the existing mutation authority. Startup examines blocked jobs before claiming pending work and repairs a missing or stale blocked sidecar without entering ASR. A fault after the ledger write can leave presentation stale, but cannot turn compute back on; fault-point tests pin that direction.

Backward compatibility is bounded and signature-conservative. Status immediately recognizes retained legacy `FAILED` rows with known CUDA/cuBLAS/cuDNN/driver prefixes and presents compute-runtime remediation rather than artifact repair. At worker startup, a bounded reconciliation batch first marks those rows blocked, then converges their canonical sidecars through the same ledger-first path without transcription. Unknown exceptions and ambiguous historical messages stay on the generic failed-job path rather than being falsely relabeled. The source artifact is never touched.

Alternatives considered: publishing the sidecar first preserves the measured crash window where startup turns a running row pending and retranscribes; claiming cross-store atomicity is false; string matching only in status leaves the durable ledger semantically wrong; eagerly migrating every old failure risks reclassifying corrupt inputs; swallowing the error makes successful indexing unverifiable.

### 6. Verification measures active work, not just idle cleanup

Unit tests begin red for budget parsing, early environment installation, framework API arguments, scheduling calls, service template limits, Blackwell-safe compute selection, CUDA error classification, and job/sidecar consistency. Tests use fakes and subprocess entrypoint probes so they do not initialize real model stacks.

The resource-envelope verifier gains an active-cgroup phase for Linux systemd hosts. It renders the production quota, starts an isolated transient unit containing a real Exomem sample-vault server plus a deliberately non-cooperative background child that ignores thread variables, and rate-samples aggregate process-tree CPU while probing health/status. Over a five-second window, aggregate CPU may exceed the rendered quota by no more than 0.25 core and every probe must complete within one second. Thread count is diagnostic only; runnable CPU rate and request latency are the acceptance measures. Deterministic tests inject process metrics, while the real transient-unit gate is required on a systemd release host and reports unsupported rather than passing when the facility is unavailable. The existing idle-worker and no-residency checks remain. A GPU completion gate additionally runs the tiny real ASR probe on representative hardware; CI without a GPU skips only that hardware assertion, not the selection and failure-state contracts.

### 7. Sidecar ownership is explicit; duplicate cleanup needs a known extraction

The affected hosted cell retained a 9.5 MB sidecar for a 38 KB DOCX. It contained one valid 11,627-character extraction followed by 720 byte-identical copies of that extraction under `## Preserved notes`, separated only by whitespace. This is derived duplication, not 720 authored notes, and it kept one media job runnable for hours while catalog publication processed the inflated page.

New sidecars therefore mark machine-owned `Artifact` and `Preserved notes` boundaries with reserved HTML sentinels. MarkItDown and other document renderers legitimately emit H1/H2 headings—including those exact titles—inside the payload, so headings alone cannot prove ownership. A complete terminal legacy artifact block remains recognizable by its exact generated shape. A nonempty unmarked legacy notes boundary is ambiguous: extraction commit stops before metadata or queue mutation, the durable job becomes blocked, and status tells the operator to review that boundary.

Legacy audit repair may discard a preserved-note residual only when an actual surviving extraction supplies the byte-exact candidate and removing whole occurrences leaves whitespace and nothing else. A blank top extraction does not supply a candidate: repeated bytes alone cannot distinguish many generated copies from fewer authored compound units, so audit repair reports the damage but leaves the file unchanged. The next fresh source extraction is authoritative and may collapse the residual only when it is at least three exact copies of that fresh extraction separated solely by whitespace. Two copies or any differing non-whitespace byte remain preserved. Frontmatter remains verbatim, a selected extraction cannot shrink, and every repair is idempotent.

Alternatives considered: truncating at the first heading loses legitimate document sections; dropping all preserved notes loses authored recovery material; fuzzy or line-level deduplication can erase deliberate repetition; inferring a unit from a blank-top repeated run is mathematically ambiguous and can collapse an authored compound document to one smaller period. Requiring a surviving or freshly generated extraction candidate keeps the cleanup exact.

## Risks / Trade-offs

- [The one-thread native default, eight-request server limit, and serialized model gate reduce CPU-only batch throughput] → Keep the general overrides independent, simple, and observable; safety is the default and dedicated index hosts can raise them deliberately.
- [Model admission can return retryable busy under a burst] → Derive admission from the sync pool, cap it at four while reserving at least half/one general worker, and let durable background work defer rather than multiply compute.
- [Replacing inherited library variables can surprise an operator] → Make `EXOMEM_CPU_THREADS` the supported override and retain library-specific behavior only behind an explicit status-visible unsafe escape hatch.
- [The half-host/four-core quota may be too low for an operator's dedicated Linux server] → Document the drop-in override and keep framework budgets separately configurable.
- [CTranslate2 capability reporting can admit a broken quantized path] → Use the conservative CUDA `float16` product default, report the resolved runtime policy, and require real model compute in the release probe.
- [Bundled NVIDIA wheels increase the media install size] → Keep them in the optional media extra; lean and CPU-only runtime behavior stays valid when no accelerator is present.
- [CUDA failures are reported through messages whose exact wording can change] → Confine conservative recognition to one typed boundary and fall back to generic failure rather than misclassifying uncertain errors.
- [Ledger-first failure publication can briefly leave a pending sidecar beside a blocked row] → Startup and status treat the ledger as the compute authority and converge presentation without running the model.
- [Priority APIs vary by OS and can be denied] → Make priority lowering best-effort and observable; correctness and the portable thread budget do not depend on it.
- [Exact duplicate cleanup could be mistaken for note cleanup] → Require a surviving or freshly source-derived extraction candidate, whole-extraction byte identity, and whitespace-only residue; preserve blank-top ambiguity and every differing non-whitespace byte.

## Migration Plan

1. Ship the runtime budget, child priority, service-template backstop, diagnostics, and tests without removing existing machine-local drop-ins.
2. Ship the ASR computation/runtime packaging, typed blocked-state repair, and bounded legacy reconciliation, then upgrade the affected native installation through the normal stop-window deployment path.
3. Before starting production service work, run doctor and the explicit child-process GPU model-execution probe in the target service interpreter. On failure, keep ASR on the explicit bounded CPU path or leave affected jobs blocked; do not retry CUDA jobs blindly.
4. In a controlled stop window, preview and apply the governed sidecar repair to exact machine-generated duplicates before allowing the affected worker to publish or index them again.
5. Start the service, allow startup to reclassify retained accelerator failures, process one representative audio artifact, and measure process-tree CPU, health/status latency, job/sidecar state, selected device/type/runtime, and worker exit.
6. Reduce or remove the workstation's temporary quota drop-ins only after the packaged limits and measurements prove host responsiveness. Roll back by restoring the previous release while retaining the stricter local drop-ins; durable blocked jobs remain retryable and source artifacts require no migration.
