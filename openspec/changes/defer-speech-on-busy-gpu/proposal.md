## Why

With `EXOMEM_ASR_DEVICE=cuda`, the speech stage admits CUDA only when `nvidia-smi` reports 2048 MiB or more of free VRAM. A GPU that another process holds at that moment failed the check. The stage then refused the job, and Exomem blocked it with the advice to repair the CUDA runtime. The media child kept that failure for its whole life and blocked every later audio or video job too. Each blocked job needed a manual retry.

A busy GPU is a passing state, not a broken runtime. The person who uses Exomem should never notice it.

## What Changes

- **Busy and broken are two conditions.** A GPU that works but has too little free VRAM is *busy*. A missing CUDA device, an unusable `nvidia-smi`, a failed library load and an unsupported computation type stay *broken*, and a broken runtime still blocks the job.
- **A busy GPU defers speech jobs.** The job stays pending with no error and no attempt counted. The media child passes over the other speech jobs, runs the rest of the queue, and exits. The supervisor starts the next child one minute later, and that child asks the GPU again.
- **No CPU fallback.** An explicit `cuda` choice stays explicit. Automatic selection already chooses CPU when VRAM is short, and it does not change.
- **One `nvidia-smi` lookup.** The ASR probe and the resource status share one function that tries `PATH` and then the WSL driver location, so a service without that location on `PATH` still finds the tool.

## Capabilities

### Modified Capabilities

- `multimodal-job-runtime`: an explicit CUDA request waits for a busy GPU and refuses only a broken runtime. The scenarios "No usable accelerator" and "Explicit CUDA never falls back silently" name the broken cases only.

## Impact

- Code: `src/exomem/asr_runtime.py` (busy probe state and `ASRGpuBusy`), `src/exomem/media_worker.py` (deferral, exit code, supervisor delay), `src/exomem/media_jobs.py` (`claim_next` can pass over media types), `src/exomem/media_engines.py` (public `SPEECH_KINDS`), `src/exomem/resource_status.py` (public `find_nvidia_smi`), `src/exomem/extract.py` (prewarm logs a busy GPU at info level).
- Data: none. A deferred job keeps its ledger row and sidecar unchanged.
- Behaviour: a busy GPU delays speech jobs by up to one minute after it frees up. Inline media mode (tests and debugging) logs the busy error and does not retry.
