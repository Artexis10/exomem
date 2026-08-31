## Why

Legitimate media and indexing work can consume nearly every host CPU through independent native thread pools, making a self-hosted desktop and even WSL control traffic unusable. The disposable-worker contract bounds residency and process count but does not bound active compute, while the CUDA ASR path forces an INT8 mode that current CTranslate2 disables on Blackwell and reports accelerator failures as corrupt input.

## What Changes

- Add conservative, operator-overridable native-thread and synchronous-request budgets that are applied before native runtimes initialize and passed explicitly to CTranslate2, PyTorch, ONNX Runtime, BLAS/OpenMP, tokenizer runtimes, and the server's AnyIO worker limiter where supported; bound admitted model work separately so it cannot consume every general request worker.
- Run disposable media work at background scheduling priority and render a Linux service cgroup backstop that reserves at least half of the host, capped at four Exomem cores, so CPU-only or degraded hosts remain interactive under legitimate indexing load.
- Let normal-mode disposable media workers use a healthy accelerator without creating persistent server residency; quiet mode and CPU-only hosts remain bounded CPU paths.
- Select a CTranslate2-supported CUDA compute type instead of forcing `int8_float16`, and bind Linux media installs to the CUDA 12 runtime CTranslate2 actually consumes rather than treating the unrelated PyTorch CUDA runtime as sufficient.
- Classify CUDA/cuBLAS/runtime failures as blocked compute-runtime failures with actionable remediation, converge the durable job and sidecar crash-consistently, repair retained legacy failure classifications on upgrade, and never blame or rewrite the source artifact for an accelerator failure.
- Expose the effective CPU budget, scheduling posture, ASR device/compute policy, and accelerator refusal through no-allocation diagnostics and release verification.
- Keep ASR as deterministic pure-substrate transcription. It remains demand-loaded, soft-fail for the core service, and default-off only when the existing media-extraction capability is disabled.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `resource-governance`: Bound active native compute and background scheduling in addition to the existing idle persistent-core envelope.
- `multimodal-job-runtime`: Route disposable workers to healthy accelerators, bound CPU execution, and preserve truthful failure/job state when compute infrastructure fails.
- `install-readiness`: Install and verify the runtime dependencies required by the selected ASR accelerator instead of inferring readiness from an unrelated framework.
- `command-surface`: Report the effective compute envelope and accelerator remediation without loading model stacks.

## Impact

- Runtime: process bootstrap, disposable media child launch, faster-whisper/CTranslate2 construction, ONNX/PyTorch thread configuration, scheduling priority, media failure classification, and resource status.
- Installation: Linux/Windows media extras and native service templates; existing operator overrides remain authoritative.
- Verification: deterministic thread-budget and failure-state tests, installer contracts, active-worker resource measurements, and a real Blackwell transcription probe where that hardware is available.
- Compatibility: no user-authored schema change and no automatic CPU fallback after an accelerator execution failure; CPU-only hosts continue to work through the bounded CPU path.
