## 1. Busy is not broken

- [x] 1.1 Add a distinct retryable condition for a GPU with too little free VRAM; keep the blocking refusal for a missing device, a card that is too small, an unusable probe and an unsupported computation type.
- [x] 1.2 Defer the job when the child meets it: leave it pending with no error, pass over other speech jobs, run the rest of the queue, and exit with a dedicated code. Treat a CUDA out-of-memory error at model load as busy for an explicit CUDA request.
- [x] 1.3 Make the supervisor wait a bounded interval after that exit code, and end the wait when media that needs no GPU is queued.
- [x] 1.4 Report the wait in status and doctor: count, reason with free and needed MiB, and since when.

## 2. One nvidia-smi lookup and one memory knob

- [x] 2.1 Make `resource_status.find_nvidia_smi` the single lookup (`PATH`, then the WSL driver location) and call it from the ASR probe.
- [x] 2.2 Read the minimum free VRAM for ASR admission and for doctor from `EXOMEM_GPU_MIN_FREE_GB` alone.

## 3. Proof

- [x] 3.1 Cover the probe, the child (busy defers and a later attempt runs the jobs; an image behind a deferred recording still runs; out of memory at load defers) and the supervisor (queued media ends the wait).
- [ ] 3.2 After the release is installed, set `EXOMEM_ASR_DEVICE=cuda`, hold the GPU with another process, queue one recording, and record that the job stays pending and completes after the GPU frees up.

## 4. A CUDA fault never takes down the serving worker

- [x] 4.1 Start every worker, standby and migrator that the service manager owns with no CUDA device, and keep the deployment's device list for the media child.
- [x] 4.2 Run a preflight in a throwaway process once per worker lifetime; deny the media child the GPU when it dies by a signal, times out or fails, and name the failure in the blocked job, status and doctor.
- [x] 4.3 Run a torch model that asks for CUDA on the CPU in a worker whose devices are hidden.
- [x] 4.4 Restart a media child that dies by a signal after a doubling delay.
- [x] 4.5 Hide the devices in the server entry point as well, so a foreground or standard-input serve is covered, and keep the hiding idempotent.
- [x] 4.6 Run the preflight with the media child's interpreter flags and environment, bound its wait after a kill, and report a host with nothing to test as "not tested".
- [x] 4.7 End a GPU wait when any path queues work that needs no GPU, record the preflight verdict and the crash loop in status, and keep these facts out of a Cloud cell's tool status.
- [x] 4.8 Run doctor's GPU probe in a throwaway process, show the crash loop in a check of its own, and disclose the CPU move only where an NVIDIA driver exists.
- [x] 4.9 Record the preflight verdict and the CUDA split at the first child launch, and let a supervisor start change only what is stale.
- [ ] 4.10 After the stale NVIDIA package is repaired on the affected host, install the release, confirm that the preflight passes and that the worker stays up, and run one speech job on the GPU.
