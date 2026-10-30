## 1. Busy is not broken

- [x] 1.1 Add a distinct retryable condition for a GPU with too little free VRAM; keep the blocking refusal for a missing device, an unusable probe and an unsupported computation type.
- [x] 1.2 Defer the job when the child meets it: leave it pending with no error, pass over other speech jobs, run the rest of the queue, and exit with a dedicated code.
- [x] 1.3 Make the supervisor wait a bounded interval after that exit code before it starts the next child.

## 2. One nvidia-smi lookup

- [x] 2.1 Make `resource_status.find_nvidia_smi` the single lookup (`PATH`, then the WSL driver location) and call it from the ASR probe.

## 3. Proof

- [x] 3.1 Cover the probe (low VRAM is busy, an unusable probe is a refusal, the WSL location is found) and the child (busy defers and a later attempt runs the jobs; an image behind a deferred recording still runs).
- [ ] 3.2 After the release is installed, set `EXOMEM_ASR_DEVICE=cuda`, hold the GPU with another process, queue one recording, and record that the job stays pending and completes after the GPU frees up.
