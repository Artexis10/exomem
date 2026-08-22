## 1. Agent And Planning Guardrails

- [x] 1.1 Add `AGENTS.md` as the shared non-Claude entry point to the canonical repo instructions.
- [x] 1.2 Tighten the repo instruction that new implementation work starts in an isolated worktree.
- [x] 1.3 Validate the OpenSpec change artifacts.

## 2. Doctor And Native Determinism

- [x] 2.1 Fix human-readable doctor output so Windows legacy consoles cannot crash on non-ASCII advisory text.
- [x] 2.2 Add doctor/runtime checks that report native versus container runtime, compute mode, selected device, CUDA availability, and CUDA residency when detectable.
- [x] 2.3 Harden Windows service install/restart scripts so selected hybrid/media profiles fail before success when required extras are missing.
- [x] 2.4 Add focused tests for terminal-safe output and missing-extra native gates.

## 3. Container Runtime Profiles

- [x] 3.1 Add a CUDA-capable Dockerfile target that packages CUDA-capable deterministic measurement dependencies without making CUDA resident by default.
- [x] 3.2 Extend the release workflow to publish `cuda` and immutable `X.Y.Z-cuda` tags.
- [x] 3.3 Extend Compose runtime selection for lean, CPU-ML, and CUDA with explicit NVIDIA device wiring only on the CUDA path.
- [x] 3.4 Add Docker smoke/build checks that cover the lean path cheaply and validate CUDA target metadata without requiring a CI GPU.

## 4. Cross-OS Setup UX And Docs

- [x] 4.1 Update Docker docs to explain lean, CPU-ML, and CUDA images plus Windows/macOS native recommendations.
- [x] 4.2 Update deployment/quickstart docs so one-command setup chooses or recommends native Windows, native macOS, Linux CUDA Docker, or CPU Docker appropriately.
- [x] 4.3 Ensure setup/doctor wording distinguishes CUDA-capable from CUDA-resident and keeps normal mode as CPU-default.

## 5. Verification

- [x] 5.1 Run OpenSpec validation for the change.
- [x] 5.2 Run focused unit tests for doctor/setup/service-script behavior.
- [x] 5.3 Run lint or targeted static checks for changed Python files and shell/PowerShell scripts where applicable.
