## Why

Exomem now has multiple legitimate runtime shapes: native Windows for live-vault
watching and GPU/MPS paths, lean Docker for low-friction trials, CPU-ML Docker for
reproducible hybrid search, and a missing CUDA Docker path for NVIDIA Linux/WSL2.
The setup experience should select and verify the right shape per host so users get
one-command installation without recreating the prior idle VRAM/RAM contention failure.

## What Changes

- Add a CUDA-capable Docker variant and publish immutable CUDA tags alongside the
  existing lean and CPU-ML images.
- Add Docker Compose runtime overrides that distinguish lean, CPU-ML, and CUDA
  choices, including `/data` persistence for logs/model caches and explicit NVIDIA
  device wiring only for CUDA.
- Extend install/setup guidance so Windows native remains the recommended live-vault
  path, Linux+NVIDIA can choose CUDA Docker, Windows+WSL2 can choose CUDA Docker with
  watcher tradeoff disclosure, and macOS Apple Silicon stays native for MPS/MLX.
- Harden `doctor`/setup reporting so it names the actual runtime, dependency profile,
  compute mode, selected device, and whether CUDA is only available or actually resident.
- Preserve the resource-safe default: CUDA-capable installs MUST still boot in normal
  CPU-default mode unless the user explicitly opts into performance/GPU use.

## Capabilities

### New Capabilities

- `container-runtime-profiles`: Docker image, tag, and Compose-runtime behavior for
  lean, CPU-ML, and CUDA-capable deployments.

### Modified Capabilities

- `install-readiness`: setup and doctor requirements for cross-OS runtime selection,
  native-vs-container recommendations, and deterministic dependency/profile gates.

## Impact

- `Dockerfile`, `compose.yaml`, `.github/workflows/release-please.yml`, and Docker docs.
- Setup/doctor CLI paths and tests around dependency/runtime reporting.
- Native Windows service docs/scripts where they must enforce locked extras and avoid
  silently starting a degraded hybrid install.
- No server-side reasoning model is introduced; Docker CUDA only packages deterministic
  measurement models already used by embeddings/CLIP/ASR paths and remains default-off
  for GPU residency through the existing mode system.
