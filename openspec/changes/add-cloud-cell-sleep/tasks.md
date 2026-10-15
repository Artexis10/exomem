## 1. Fast wake (Exomem; ships first, helps every restart)

- [ ] 1.1 Run the read warm (lexical caches, recall encoder preload) beside the graph handoff and the semantic corpus on a Cloud cell; keep writes gated on both
- [ ] 1.2 Read the semantic corpus state from the volume when current, instead of rebuilding it on every start
- [ ] 1.3 Prefetch the state and model files in the cell's init container
- [ ] 1.4 Measure start to first complete answer on the owner-vault copy (warm and cold page cache, n=5) and record it against the 20 s p95 target

## 2. Activity and quiescence (Exomem)

- [ ] 2.1 Add `activity`, `last_request_at` and `wake_requested_at` to `cells`, with the gateway's column grant and the `NOTIFY` trigger on `wake_requested_at`
- [ ] 2.2 Report `quiescent` in the cell's `/health/ready`
- [ ] 2.3 cellctl: render replicas from desired state and activity; sleep idle quiescent cells; wake on request; idle period from chart values with a per-cell override and never-sleep
- [ ] 2.4 cellctl: backups and upgrades on asleep cells (D6)
- [ ] 2.5 cellctl: publish total and awake capacities; LRU eviction and the arrival-order wake queue (D7)

## 3. Gateway and admission (Substrate)

- [ ] 3.1 Record `last_request_at` (coalesced) and write wake requests
- [ ] 3.2 Hold requests to waking cells up to the hold budget, then answer `CELL_WAKING` with a retry delay
- [ ] 3.3 Answer `initialize` and `tools/list` for asleep cells from responses recorded per image, and pre-wake
- [ ] 3.4 Admission counts non-deleted cells against total capacity
- [ ] 3.5 Tenant home and operator status for sleep, through the lifecycle status registry

## 4. Rollout

- [ ] 4.1 Canary on the QA cell with a short idle period: sleep, wake within budget, cached discovery, backup while asleep; record timings
- [ ] 4.2 Enable sleep for friend cells; the owner's cell stays never-sleep until the owner chooses
