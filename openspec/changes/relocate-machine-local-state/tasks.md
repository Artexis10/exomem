# Tasks

## 1. Regression tests (red first)

- [x] 1.1 Placement inventory pins: independently enumerate source-described
      persistent families and registered descriptors; require every family to
      have exactly one placement (`vault-canonical`, `external-state`, or
      `target-adjacent`) so an omitted constructor map and omitted registry row
      cannot bless each other.
- [x] 1.2 Seam/atomicity pins: an absolute `EXOMEM_STATE_ROOT` relocates every
      external-state consumer at once and a relative override fails;
      reject a resolved root inside its own vault before cached/manifest
      admission or owner I/O; mutation-proof the seam. Separately prove
      batch/held scratch stays beside its target on the same volume and is not
      migrated.
- [x] 1.3 Migration pins: fresh-vault build in external root; existing
      in-vault state creates an in-progress manifest before moving and reaches
      complete; interrupted migration resumes with both roots present;
      unexplained dual-state refuses; completed-manifest leftovers remain
      startup-refusing and doctor-failing; an active legacy WAL writer remains
      authoritative and can commit after target readiness refuses; a newer
      descriptor set is migrated only by a later offline invocation before
      consumers open it, without blessing unexplained destination-only bytes;
      migration never deletes unmoved bytes (cross-volume copy-verify-delete,
      durable tree removal and crash-replay path included); normalized,
      key-bound manifest records reject traversal/absolute-path tampering;
      invalid/unreadable/newer manifests fail closed; concurrent migrators are
      serialized.
- [x] 1.4 Portability pins: a vault with no external root builds regenerable
      state fresh; external graph receipts/review decisions and every other
      `PORTABLE_DERIVED` family remain in schema-v1 export/restore; restore owns
      request-bound offline relocation before readiness, writes an empty
      complete manifest when necessary, survives crash replay, forbids
      wrong-side/dual placement, and does not depend on deployment
      `migrationMode`.
- [x] 1.5 Fixture isolation: no test writes the real user state root — every
      fixture injects sibling vault/external roots and places writer-lease state
      beneath the same isolated test root (assert via a guard fixture, mirroring
      the vault-path isolation rule).
- [x] 1.6 Privacy/hosted pins: public `/health` exposes no absolute path;
      health performs no vault enumeration; doctor owns detailed placement;
      hosted binding sets `EXOMEM_STATE_ROOT` beneath the private hosted root
      without changing the writer-lease seam; symlink/junction escape and
      private-root/DACL preparation failure are fatal.

## 2. Implementation

- [x] 2.1 Add the state-root resolver + vault-identity key and replace
      `machine_local` with explicit `StatePlacement`; keep portability
      orthogonal and the registry the closed name authority.
- [x] 2.2 Route every external persistent family through the seam: governance
      SQLite, embeddings, CLIP, lexical store/rebuild/quarantine, graph
      store/rebuild/reset, claims, refs and legacy references/freshness,
      deferred-index, media-jobs, legacy in-vault idempotency, voice profiles,
      graph handoff/receipts, due/review, authorization projections and
      measurements, and legacy `.graph-coordination`.
- [x] 2.3 Preserve target-adjacent batch/held publication mechanics and bounded
      cleanup/recovery; do not route them through the external root.
- [x] 2.4 Implement the centralized one-time migration gate with versioned
      in-progress/complete descriptor manifest, interprocess lock, held-handle
      moves, durable copy-verify-delete, resumable per-family progress,
      descriptor-set upgrades, unexplained dual-state refusal, and leftover
      quarantine classification. Ordinary callers use only the read-only gate;
      mutation, resume, adoption and descriptor upgrade require explicit
      offline authority.
- [x] 2.5 Add detailed doctor placement and path-free `/health` status.
- [x] 2.6 Bind hosted `EXOMEM_STATE_ROOT` beneath the existing private hosted
      root; reject relative roots; fail closed on private-root preparation;
      preserve `EXOMEM_WRITER_LEASE_STATE_DIR` behavior.
- [x] 2.7 Update hosted portability to export/restore relocated
      `PORTABLE_DERIVED` families from their external paths while retaining
      schema-v1 logical names; make restore itself relocate and exact-verify
      split placement under the lifetime lock, journal manifest/placement
      proofs, replay idempotently, and repair placement-aware before READY.
- [x] 2.8 Wire the read-only readiness gate before all service and stateful CLI
      entry points can open an external family; expose mutation only through
      explicit offline maintenance and stop-window deployment paths; enforce
      the outside-vault invariant before admission and every owner I/O path.
- [x] 2.9 Scaffold/docs: update the shipped `.stignore` guidance (defense in
      depth) and CLAUDE.md live-cell guardrails to name the new root.
- [x] 2.10 Make Windows and POSIX install/upgrade one stopped transaction with
      a durable service/root/phase/captured-PID receipt, exact worker/listener
      stop and resume proof, a shared persisted state-root binding, target
      install/migration/doctor/start/listener-ownership/version ordering,
      explicit stopped-transition recovery, and fail-stopped cleanup that
      captures every failed-start worker/listener before publishing a resumable
      phase (or retains non-resumable `starting`). Make hosted rollout prove
      fresh zero-pod state and return route/commit failures to target-image zero
      with routes closed.
- [x] 2.11 Admit provably-fresh deployments without offline ceremony: when the
      manifest is absent and both the vault legacy scan and the external root
      are provably empty, the readiness gate bootstraps the first empty
      complete manifest under the migration lock (re-verifying emptiness under
      the lock); every other manifest-absent shape keeps the refusal. Repairs
      first-run onboarding — the docker smoke boots the server directly over a
      just-initialized vault and fail-closed startup broke it deterministically.

## 3. Verification and delivery

- [ ] 3.1 Focused placement, migration, hosted, portability, privacy and
      publication-atomicity suites + lint + strict OpenSpec validation, including
      the current D0/D1 governance rollback crash matrix and real old-v3 proof.
- [ ] 3.2 Independent adversarial review of the current rollback implementation
      and its D0/D1/fence evidence; resolve findings.
- [ ] 3.3 Release and deploy to personal and POLLY cells; verify exact package
      and service process, path-free health/readiness, detailed doctor state,
      clean quiescent leftover scan, restart durability, and real retrieval.
- [ ] 3.4 Unpause the personal Knowledge Base Syncthing folder only after the
      migrated cell is quiescent; verify no persistent state reappears and the
      cell continues serving steadily while sync is active.
- [ ] 3.5 Sync and archive this change after shipped evidence is complete.
