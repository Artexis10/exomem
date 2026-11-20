## 1. Coverage and dependency evidence

- [ ] 1.1 Add bounded legacy coverage reconciliation and invalidate unexplained external changes through the existing watcher.
- [ ] 1.2 Wire capture producers that reserve pre-reference residue through `capture_attempts.reserve_residue` before their bytes can survive.
- [ ] 1.3 Clear a dependency reservation that proves no effect or complete rollback, and call bounded dependency reconciliation in production.

## 2. Store-mode retirement

- [ ] 2.1 Agree the canonical store owner's purge and provenance API, then integrate bounded page, held, audit, projection and recovery cleanup.
- [ ] 2.2 Retire or replace managed store snapshots and published replicas older than the purge revision.
- [ ] 2.3 Lift `SOURCE_ERASURE_MODE_UNSUPPORTED` for store mode and report store policy from configuration.

## 3. Verification and delivery

- [ ] 3.1 Prove store purge, audit redaction, replica retirement and path reuse in an isolated store cell.
- [ ] 3.2 Qualify a progress call's p95 on the generated 20,000-Source reference against the public warm-call budget, in file and store cells.
- [ ] 3.3 Run completion checks, independent stored-data review, privacy and strict OpenSpec validation; synchronize and archive the finished change.
- [ ] 3.4 Deliver the batch through green CI and ordinary release with host erasure policy disabled.
