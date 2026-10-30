## Why

Two GPU faults reach the person who uses Exomem today.

A busy GPU. With `EXOMEM_ASR_DEVICE=cuda`, the speech stage admits CUDA only when `nvidia-smi` reports enough free VRAM. A GPU that another process holds at that moment failed the check. The stage then refused the job, and Exomem blocked it with the advice to repair the CUDA runtime. The media child kept that failure for its whole life and blocked every later audio or video job too. A busy GPU is a passing state, not a broken runtime.

A crashing CUDA library. In October 2026 a stale NVIDIA user-space package on a WSL host shadowed the driver's compiler library. Initializing CUDA in torch then ended the process with a segmentation fault. A library in the serving worker called into CUDA on every forward pass, so the worker died about every six minutes. A CUDA library can crash the process that loads it, and the serving worker must not be that process.

## What Changes

- **Busy and broken are two conditions.** A GPU that works but has too little free VRAM is *busy*. A missing CUDA device, a card whose total memory is below the minimum, an unusable `nvidia-smi`, a failed library load and an unsupported computation type stay *broken*, and a broken runtime still blocks the job.
- **A busy GPU defers speech jobs.** The job stays pending with no error and no attempt counted. A CUDA out-of-memory error while the model loads counts as busy, because the card can fill between the probe and the load. The media child passes over the other speech jobs, runs the rest of the queue, and exits. The supervisor starts the next child one minute later, or at once when media that needs no GPU is queued, and that child asks the GPU again.
- **The wait is visible.** Status and doctor report the number of waiting speech jobs, the reason with free and needed MiB, and since when.
- **No CPU fallback.** An explicit `cuda` choice stays explicit.
- **One `nvidia-smi` lookup and one memory knob.** The ASR probe and the resource status share one lookup that tries `PATH` and then the WSL driver location. Both read the minimum free VRAM from `EXOMEM_GPU_MIN_FREE_GB` (default 2 GB). The check is a cheap pre-check: it cannot know what the model needs, so an out-of-memory error at load time defers the job too.
- **A CUDA fault never takes down the serving worker.** The service starts every worker it owns with no CUDA device visible. It keeps the deployment's device list for the disposable media child only. Before the first media child of a worker lifetime, the media supervisor initializes CUDA in a throwaway process. If that process dies by a signal, times out or fails, the child gets no GPU. An explicit `cuda` request then blocks with the failure named, an automatic request uses the CPU, and status and doctor show the reason.
- **A crashing media child backs off.** A child that dies by a signal the supervisor did not send is restarted after a delay that doubles from 2 seconds to 5 minutes. Before, it restarted every 2 seconds without limit.

## Capabilities

### Modified Capabilities

- `multimodal-job-runtime`: an explicit CUDA request waits for a busy GPU and refuses only a broken runtime; a CUDA fault cannot reach the serving worker.

## Impact

- Code: `src/exomem/cuda_containment.py` (new: hidden device list, preflight), `src/exomem/service_manager.py` (every owned child starts without CUDA devices), `src/exomem/asr_runtime.py` (busy probe state, `ASRGpuBusy`, preflight reason), `src/exomem/media_worker.py` (deferral, exit code, supervisor delays, preflight, crash back-off), `src/exomem/media_jobs.py` (`claim_next` can pass over media types; GPU facts in status), `src/exomem/doctor.py`, `src/exomem/accel.py`, `src/exomem/media_engines.py`, `src/exomem/resource_status.py`, `src/exomem/extract.py`.
- Data: none to migrate. The store records two optional facts in its existing `meta` table.
- Behaviour, automatic mode: the shared `nvidia-smi` lookup also finds the WSL driver location, which the ASR probe did not search before. A WSL host in automatic mode can now choose CUDA when the card has the free memory and the preflight passes. Before, that host always used the CPU.
- Behaviour, serving worker: models that load in the worker run on the CPU, including a torch model that `EXOMEM_DEVICE=cuda` or performance mode asked for. Only the media child uses the GPU. Inline media mode runs in the worker and so has no GPU. A foreground `exomem serve` run without the service manager keeps its devices.
- Behaviour, busy GPU: a speech job waits up to one minute after the GPU frees up. Inline media mode logs the busy error and does not retry.
