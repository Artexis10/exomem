## Context

Exomem currently ships a lean Docker image and a CPU-only `ml` image. The live
Windows desktop deployment is native NSSM over the repo `.venv`, because that path
preserves direct vault file-watching and GPU/MPS-capable local runtimes. A recent
incident showed the native path can silently lose optional extras when a plain
`uv sync` strips `sentence-transformers`/`torch`; a separate earlier incident showed
that eager CUDA residency can steal enough VRAM to affect games and other desktop
workloads.

The product needs a low-friction cross-OS setup story without forcing one runtime
everywhere. Linux users with NVIDIA should get a reproducible CUDA container. Windows
live-vault users should keep native as the default. macOS Apple Silicon should keep
native for MPS/MLX. All paths must preserve the pure-substrate boundary: the packaged
models are deterministic measurement components, not server-side reasoning.

## Goals / Non-Goals

**Goals:**

- Add an NVIDIA CUDA Docker variant that can run hybrid search and media paths with
  CUDA available.
- Keep all installs resource-safe by default: normal mode remains CPU-default and
  CUDA is explicit opt-in through mode/device/bulk operations.
- Make Docker Compose express lean, CPU-ML, and CUDA runtime choices clearly.
- Make setup/doctor report the runtime and compute profile accurately enough to
  catch missing extras, missing NVIDIA runtime, and unintended GPU residency.
- Preserve native Windows as the recommended live-vault/GPU path while making
  Linux CUDA Docker a first-class one-command path.

**Non-Goals:**

- Do not make Docker the only supported deployment path.
- Do not add a server-side reasoning model.
- Do not make the CUDA image the default `latest` image.
- Do not solve full auto-quiet in this change; this change should leave a clean
  hook point and not regress the existing `quiet|normal|performance` mode system.

## Decisions

1. **Publish three image families.**
   Keep `lean` as `latest`/`X.Y.Z`; keep CPU hybrid as `ml`/`X.Y.Z-ml`; add CUDA
   hybrid as `cuda`/`X.Y.Z-cuda`. Alternative considered: replace `ml` with CUDA.
   Rejected because CPU-only hybrid remains smaller, easier to run anywhere, and
   safer for hosts without NVIDIA runtime.

2. **CUDA image contains CUDA capability, not CUDA residency.**
   The CUDA image may include CUDA torch/runtime libraries, but it still boots in
   normal CPU-default mode. Users opt into GPU via `EXOMEM_MODE=performance`,
   `exomem mode performance`, explicit device env, or bulk indexing. Alternative:
   have the CUDA tag imply performance mode. Rejected because it recreates idle VRAM
   contention and breaks the established quiet-mode product principle.

3. **Compose overrides select runtime shape.**
   `docker compose up` remains lean. `compose.ml.yaml` and `compose.cuda.yaml`
   override only the `exomem` service image/runtime fields, so they do not start
   a second server alongside the default service. The CUDA override declares
   NVIDIA device access. Alternative: mutually-exclusive Compose profile services.
   Rejected because profile services would start alongside the profile-less default
   service unless every command changed, making accidental double servers likely.

4. **Native setup becomes deterministic through gates, not by being replaced.**
   Windows service install/restart should fail early when the requested profile lacks
   dependencies, and docs/scripts should use locked extras for hybrid/media profiles.
   Alternative: switch Windows to Docker by default. Rejected because Docker Desktop
   Windows bind mounts can miss watcher events and Docker Linux containers cannot use
   Windows-native GPU/media affordances as cleanly as the native service.

5. **Doctor owns deployment truth reporting.**
   Doctor/setup should name runtime (`native`, `docker`), dependency profile, compute
   mode, selected device, CUDA availability, and whether CUDA was initialized/resident
   when detectable. Alternative: leave this in prose docs. Rejected because the prior
   failure was only obvious after inspecting logs and timings.

## Risks / Trade-offs

- **Large CUDA images** -> publish CUDA as opt-in tags and keep lean/CPU-ML available.
- **NVIDIA runtime variance across Docker Desktop, WSL2, and Linux** -> doctor reports
  the exact runtime/device state and Compose docs include verification commands.
- **CUDA context floor after performance use** -> default normal mode avoids creating
  the context at idle; docs explain restart/quiet implications when performance has
  been used in-process.
- **Windows watcher tradeoff in Docker** -> setup recommends native for Windows
  live-vault use and only offers WSL2 CUDA Docker with explicit disclosure.
- **Dependency drift in native service** -> service install/restart gates run the
  relevant doctor profile before declaring success.
