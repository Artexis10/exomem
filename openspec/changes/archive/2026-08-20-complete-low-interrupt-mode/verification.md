# Desk-Side Verification Notes

Date: 2026-07-06
Change: `complete-low-interrupt-mode`

## Idle VRAM / RAM Check

1. Start the server in normal mode and capture baseline process memory:
   ```powershell
   exomem mode normal
   exomem status --resources --json
   nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
   Get-Process python,exomem -ErrorAction SilentlyContinue |
     Select-Object Id,ProcessName,WorkingSet64,PrivateMemorySize64
   ```

2. Switch to quiet and wait one idle-unload interval or trigger live mode apply by
   restarting the server:
   ```powershell
   exomem mode quiet
   exomem status --resources --json
   nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
   Get-Process python,exomem -ErrorAction SilentlyContinue |
     Select-Object Id,ProcessName,WorkingSet64,PrivateMemorySize64
   ```

3. Expected quiet posture:
   - `policy.defer_expensive_indexes=true`
   - `policy.preload_models=false`
   - `policy.preload_cpu_caches=false`
   - `policy.retain_cpu_caches=false`
   - `models.embeddings=false`, `models.reranker=false`, `models.clip=false` unless
     a request actually loaded one
   - `cuda.initialized=false` when the process has not already opted into CUDA
   - `deferred_work.semantic_upserts.count` may be nonzero after edits; run
     `exomem index --vault <vault>` or `kb reconcile` to heal it

Acceptance target: idle Exomem should not appear as a material CUDA compute app
when no GPU feature is in use, and resident RAM should fall after quiet cache
unload compared with warm normal/performance state. Exact MB values are
machine/vault dependent; record before/after values in the PR or release note.
