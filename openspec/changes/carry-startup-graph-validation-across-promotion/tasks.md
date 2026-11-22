## 1. Carry start-up validation across promotion

- [x] 1.1 Add failing promotion tests for the guarded observation bridge, equal-token source movement, and a first unrelated write before the first read. Prove clean and residue carry and exact edges after convergence. Test an unrelated first write during cold, missing-observer and deadline fallback; no graph publication may bypass source proof.
- [x] 1.2 Add failing contention tests with a real unacknowledged checkpoint and a holder released after the former budget. Preserve cancellation and stable free-boundary repair; assert no suspension or rebuild caused by contention.
- [x] 1.3 Implement the bridge in the existing standby, runtime, watcher and graph owners. Reuse D4 guards and exact resolver/read-proof binding, preserve buffered events through seed, and keep outside repair fenced during normal governed publication. Extend the seed handoff arm through actual startup and the first ten incremental writes; document carried state and pending repair.

## 2. Let the drain retire the observations it re-derives

- [x] 2.1 Add failing tests for a scoped observation reconstructed in a replacement process with durable receipts. Cover guarded same-stat retargeting, deletion versus failure, a 64+1 checkpoint across restart, partial commits, offline edits to covered and outside paths, newer marks, publication failure and blocked-to-busy recovery. Preserve quarantine availability only after exact held settlement. Cover legacy witnesses, newer work, crash recovery and the permission-failure grace period. Assert exact acknowledgement, fencing, consumer state and rebuild equivalence without periodic reconcile assistance.
- [x] 2.2 Implement guarded observed-path publication through the existing corpus, freshness, lexical, resolver and inbound owners. Add bounded checkpoint coverage with source witnesses in graph metadata, exact post-commit path/epoch retirement and receipt revision CAS. Revalidate cold partial state before startup rebuild and requeue its proved residue. Bind held provenance at the existing quarantine CAS, preserve last-readable warnings and report affected writes as pending. Keep weak cold witnesses and unreadable external observations unproved. Complete covered debt before barrier fallback; preserve cold, unscoped, incompatible and uncovered recovery. Run scoped promotion, watcher, graph queue, drain, freshness and product integration tests.

## 3. Re-prove a moved snapshot at promotion

- [x] 3.1 Add failing tests: a snapshot published after the standby's proof promotes as current with the graph handoff carried, re-reading only the moved page; the promoted worker's first ten writes are admitted and served incrementally, and the drained graph matches a fresh rebuild; a re-proof that runs out of time promotes as advanced and the full handoff converges.
- [x] 3.2 Implement in `epistemic_graph.py` (seeded and deadline-bound proof), `service_standby.py` (the promotion re-proof) and `service_manager.py` (the shared promotion request bound); document the record fields in `docs/managed-service-upgrades.md`.
- [x] 3.3 Fit the re-proof to the production vault: walk the vault once per round for both memberships, have the supervisor send its promotion request budget and the standby bound its re-proof by a share of it, size that budget from a re-proof measured at 13,000 pages, and stop a background proof when promotion starts.

## 4. Closure

- [x] 4.1 Review the exact batch independently, resolve findings and recheck changed content. Verify the 13,000-page proof budget and before/after boundary work, preserve store-mode and rollback behavior, pass required PR checks, and merge under standing delivery authority.
- [ ] 4.2 Release through the existing publisher and perform authorized managed-upgrade acceptance with a governed write during startup. Record promotion-to-admitted-graph and unjustified rebuild count. Run the installed-wheel product restart step ten times in isolated state, using server-side evidence. Synchronize and archive only after the required deployed outcome is verified.
