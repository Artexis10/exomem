# Tasks

- [ ] Measure whether `nvidia-smi --query-compute-apps=pid,used_memory`
      attributes WSL CUDA usage. Record the output either way; it selects the
      attribution mechanism.
- [ ] Measure `utilization.gpu` from WSL with a Windows game running. If it
      does not track host load, stop and revisit the sensor before building on
      it.
- [ ] Implement attribution so Exomem's own GPU work never reads as co-tenant
      pressure.
- [ ] Add a red-first test proving a performance-mode cell with Exomem's own
      work on the GPU does NOT enter quiet, and that a mode restore cannot
      re-arm its own trigger.
- [ ] Bound the pressure floor against Exomem's real need and clamp it strictly
      below `total_mb`; test at 2 GB, 4 GB, 16 GB and 80 GB, choosing sizes
      where the bounding arms differ numerically.
- [ ] Report an unreadable utilisation signal as unreadable, not as `capable`.
      Reorder the query so `name` is last.
- [ ] Provide an explicit disable sentinel for the utilisation arm, and reject
      or clamp values outside `(0, 100]`.
- [ ] Mutation-test every threshold boundary (`>=` vs `>`, `<` vs `<=`) and the
      env lookups.
