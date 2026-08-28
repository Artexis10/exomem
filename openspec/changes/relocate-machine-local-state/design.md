# Design — relocate machine-local state

## Settled decisions

1. **Root resolution (single seam).** One function (in or beside
   `reserved_paths`) resolves the state root, in this order:
   `EXOMEM_STATE_ROOT` env (absolute path, used verbatim) →
   `%LOCALAPPDATA%\exomem\state` on Windows →
   `$XDG_STATE_HOME/exomem/state` else `~/.local/state/exomem/state` on
   POSIX. A relative override, or an override whose resolved per-vault state
   directory is the vault or one of its descendants, is a configuration error,
   not a path relative to whichever process happened to start the service. One
   pure containment validator enforces that invariant before readiness-cache or
   manifest admission and before any owner opens state. Every `external-state`
   consumer that today writes under `vault_root / kb_dirname() / ".<name>"`
   derives its directory from this seam instead. No such consumer may compose
   the root itself — mutation-pin the seam the way `_follower_wait_seconds` is
   pinned.

2. **Vault key.** Per-vault subdirectory named
   `<slug>-<sha256(normalized resolved vault path)[:16]>`, where slug is a
   filesystem-safe tail of the vault directory name (for human
   navigability). Normalization: `Path.resolve()`, casefold on Windows,
   NFC. The same vault path on the same machine always maps to the same
   key; a *moved* vault maps to a new key and regenerates (or migrates via
   the same one-time rule below, since its old external root will not
   match).

3. **Placement is explicit and independent of portability.** Replace the
   overloaded `machine_local` boolean with `StatePlacement`: `vault-canonical`,
   `external-state`, or `target-adjacent`. The registry remains the closed name
   authority and the migration enumerates `external-state` descriptors rather
   than hand-copying names. Portability remains a separate classification: an
   external runtime family may still be portable-derived.

   - **External persistent state:** governance SQLite, embeddings, CLIP,
     lexical SQLite/rebuild/quarantine, graph SQLite/rebuild/reset, claims,
     refs plus legacy references/freshness, deferred-index, media jobs, legacy
     in-vault idempotency, voice profiles, graph epoch/floor/recovery JSON,
     graph receipts, review/due state, authorization projections and their
     measurement stores, and legacy `.graph-coordination`.
   - **Target-adjacent transaction scratch:** batch workspaces and
     held-publication temporaries. These follow the publication destination so
     rename/link remains same-volume. They may exist in the vault only during
     an active publication or bounded crash recovery and are never migrated as
     state families.
   - **Vault canonical:** notes, `_access.yaml`, the governance and
     consolidation trees, durable `_Adoption` material, and other human-owned
     artifacts intended to sync.

   Already-external service/lease state keeps its existing
   `EXOMEM_WRITER_LEASE_STATE_DIR` seam. Unifying that deployed runtime root is
   a separate migration. A source-inventory pin must complement registry
   enumeration so an omitted descriptor and an omitted constructor map cannot
   bless each other.

4. **Migration semantics (explicit offline authority, transactional per
   family, never lossy).** Ordinary service and stateful CLI entrypoints call
   `require_vault_state_ready()`, a read-only gate. It never creates a
   directory, acquires the migration lock, copies, unlinks, resumes, adopts, or
   upgrades state. It returns the stable path-free
   `STATE_MIGRATION_OFFLINE_REQUIRED` refusal for an absent or in-progress
   manifest, a complete manifest with a stale descriptor set, or a complete
   manifest plus any legacy duplicate.

   All mutation lives in `migrate_vault_state_offline(..., authority=...)`,
   reachable only from explicit
   `exomem maintain --migrate-state --offline`. The authority asserts an
   externally proven stop window. The new interprocess lock serializes only
   migrators that know it exists; no v0.64.2-or-earlier writer observes that
   lock, so the lock is never accepted as proof that legacy writers are gone.
   The offline migrator owns the versioned manifest and these transitions:

   - no manifest + empty destination → create an `in-progress` manifest before
     moving bytes, move each family with held-handle discipline (`held_fs`),
     durably mark each verified family, then mark the manifest `complete`;
   - valid `in-progress` manifest → resume only under a later explicit offline
     invocation; per-family checksums and progress distinguish that state from
     unexplained dual authority;
   - no manifest + recognized destination bytes, or source/destination state
     not explained by the manifest → refuse; doctor names both paths and the
     explicit offline `--adopt-state external|vault` remediation;
   - complete manifest + any in-vault duplicate → ordinary admission refuses;
     only explicit offline adoption may remove one authority;
   - complete manifest with an older descriptor set → ordinary admission
     refuses; the explicit offline invocation inventories source and
     destination for every newly external descriptor before updating the set.
     Destination bytes that were not established by the older manifest or an
     exact source proof remain unexplained authority and require explicit
     adoption rather than being stamped complete;
   - neither legacy state nor destination bytes → the offline invocation writes
     a complete empty manifest, after which ordinary startup may build fresh
     state;
   - migration never deletes bytes it did not first copy, verify, durably
     publish, and directory-flush. Tree-family directory removal is retained
     and every affected parent is flushed before family completion; a crash
     before that completion record resumes by converging any resurrected empty
     legacy tree. An unreadable scan or invalid manifest fails closed and cannot
     stamp completion.

5. **Sync-agent defense stays.** The `.stignore` hardening (rebuild/scratch
   and state-JSON patterns) remains as belt-and-braces for vaults that
   carry historical state or roll back to older exomem versions; the
   scaffold's ignore guidance is updated to match. But the invariant the
   spec owns is: after migration, **no persistent machine-local state lives
   under the vault at quiescence**. Target-adjacent transaction scratch is
   bounded, ignored defensively, and cleaned or surfaced after crash recovery.

6. **Observability preserves the public privacy boundary.** The unauthenticated
   `/health` endpoint reports only path-free state such as placement and
   migration status. Doctor gains the local/authenticated detail: root path,
   completion manifest, in-vault leftovers, and both-present conflict. The
   refusal vocabulary is untouched.

7. **Hosted/DACL.** Hosted environment binding sets `EXOMEM_STATE_ROOT` to a
   dedicated child of `EXOMEM_HOSTED_STATE_ROOT`; it does not rely on the
   generic per-user fallback and it does not change
   `EXOMEM_WRITER_LEASE_STATE_DIR`. The hosted cell's private DACL applies to
   the new root at creation. Root preparation fails closed if private-root
   validation fails; it must never swallow the error and create a plain
   directory. The Codex-lane caveat (restricted tokens cannot traverse the
   DACL) is unchanged in substance — the root moves, the ACL story does not.

8. **Portable-derived state remains portable.** Relocating graph commit
   receipts, review decisions, or other `PORTABLE_DERIVED` families changes
   their physical source path, not their export/restore semantics. Portability
   code enumerates those registered external families explicitly; the archive
   keeps its schema-v1 logical `Knowledge Base/...` paths and classification
   rather than encoding a target machine's physical state root. A new-machine
   adoption may regenerate only families whose portability class permits it.

   Restore owns relocation as part of its transaction; target startup and a
   later deployment migration mode are not allowed to finish it. While holding
   the request-bound hosted lifetime lock, restore publishes the verified
   canonical tree, binds the exact target state environment, and invokes the
   offline migrator unconditionally. This also writes a complete empty current
   state manifest when the archive has no portable-derived members and runs
   when deployment `migrationMode` is `none`.

   The durable `state_migrated` restore phase records the state-manifest digest
   and deterministic target placement identity. Before that phase advances,
   verification proves exact archive bytes at split placement: canonical files
   in the vault, portable-derived files under the external state leaf, no
   wrong-side duplicate, and no unregistered pre-derived external extra.
   Replay from `canonical_published` reruns the idempotent offline migrator over
   absent, partial, or already-complete state. Archive repair is placement-aware
   and cannot recreate a portable-derived legacy file in the vault. Only the
   final restore journal `complete` record plus the existing READY terminal can
   authorize promotion; a complete state manifest alone cannot.

9. **Deployment is the stop-window authority.** Before stopping, desktop
   deployment atomically persists a transition receipt outside the vault. The
   receipt binds the selected service identity, configured port, exact sticky
   state root, phase, and the complete captured worker/listener PID set. Windows
   service deployment captures both the live worker and every configured TCP
   listener, stops the service, proves SCM `Stopped`, every captured PID absent,
   and the configured listener unbound, then pins the installer/operator user's
   `%LOCALAPPDATA%` state root in the service `AppDirectory/.env`. It installs
   the target, runs the target interpreter's offline migration and doctor,
   starts, then proves the listener belongs to the new selected worker and that
   `/health.version` equals the target interpreter's installed version. Target
   CLI and LocalSystem startup consume that same exact persisted value.
   `-SkipRestart` is not a valid transition and any post-stop failure leaves the
   service stopped with the receipt retained. Before a failed target start may
   advance its receipt from a pre-accepted, non-resumable `starting` or `started`
   phase to resumable `failed`, its cleanup must durably union the selected
   target worker and every attributable listener PID observed on both the
   original and target ports into the proof set. Hidden, ambiguous or unavailable
   enumeration, or failure to publish that union, retains the same pre-accepted
   phase so resume refuses.

   POSIX install and upgrade use the selected systemd/launchd unit identity for
   worker observation, stop, start, wait, and failure cleanup. They prove every
   receipt-captured worker/listener PID dead and the configured listener
   unbound, fail closed when a listener is visible but its PID is hidden, bind
   one sticky absolute state root before target package replacement, then
   migrate, doctor, start, and prove both a new worker and the live target
   version. First entry requires a running observable worker. An explicit
   stopped-transition resume requires the exact valid receipt and re-proves its
   service identity, sticky root, complete PID set, manager state and listener;
   a bare resume flag is never authority. The receipt is cleared only after the
   full live acceptance succeeds. Failure never restarts the old writer.

   Hosted rollforward first closes and externally rejects both routes, drains
   the old runtime, and proves zero runtime pods. Its target-image Job holds the
   existing hosted lifetime lock around the offline migrator, which in turn
   holds the migration lock. Once that migration checkpoint completes,
   recovery remains on the target image with routes closed; the old image is
   never restored against migrated state. A target-confirmation, route-enable,
   or commit failure returns the workload to fresh observed zero with routes
   closed rather than leaving a partially promoted candidate.

10. **Governance break-glass rollback is an evidence-bound state machine.**
    This is the sole exception to normal relocated state placement. The normal
    state-migration manifest remains v1. A rollback adds its closed,
    version-2 `governance_rollback` marker; old relocated readers reject that
    marker and current readiness refuses it before opening governance state.
    The marker binds immutable operation/event/plan/target identities, transform
    timestamp, deterministic predecessor path and stage name, `D0`, `D1`, the
    exact terminal receipt endpoint, and immutable `schema_fence_generation`
    (a positive integer or null). For backup restore only it additionally binds
    operation-derived `backup_plan_digest` and `source_store_digest`, each an
    exact 64-hex digest; both fields are null for full downmigration. A missing
    backup artifact never weakens replay: its required result is derived from
    those marker fields. Its raw manifest hash is the CAS precondition for every
    durable phase transition:
    `prepared` → `receipt-committed` → `legacy-aligned` → `complete`. A marker
    with conflicting fields, an unknown phase, or a raw-hash CAS miss refuses.

    Both full downmigration and backup restore use one lock order: state-migration
    lock, exclusive receipt-sequence lock, governance-store identity coordination,
    then one retained SQLite transaction (`BEGIN IMMEDIATE`, or `EXCLUSIVE` for
    restore). The v3 transformation completes in that actual uncommitted
    transaction. Before its COMMIT, `D0` is calculated only through the canonical
    route: serialize that transaction, deserialize it into an isolated source,
    SQLite-backup that source into a fresh snapshot, VACUUM the snapshot, then
    take the domain-separated full snapshot digest. The prepared marker is
    raw-hash-CAS written and reread while the transaction is still held; a later
    transaction or separately transformed clone cannot substitute for it.

    The deterministic `held_fs` publication initially exposes only exact `D0`.
    It permits exactly: an absent predecessor and an exact, regular, single-link
    stage; the matching stage/predecessor identity with link count two after the
    link crash boundary; or an exact, regular, single-link predecessor after the
    stage has been removed. The two-link case removes only the recorded stage and
    fsyncs its parent. Symlinks, non-regular files, a different identity, a
    different digest, or any other link count refuse and preserve evidence.

    `D0` publication is not the terminal state. The retained transaction commits
    the exact receipt terminal and therefore creates `D1`, where the only allowed
    database difference is the verified active receipt-head transition. Recovery
    may heal a durable-terminal/head-lag only after proving the exact intent,
    one adjacent terminal, sequence/previous hashes, terminal locator, durable
    JSONL tail, and complete receipt schema/keysets; it then advances only that
    head. It clones `D1`, restores only the six mutable active-head fields to the
    recorded `D0` row, and requires the normalized full digest to equal `D0`.
    Any other mutation, competing suffix, missing terminal, or non-normalizing
    result refuses. The predecessor database is then aligned to exact `D1` under
    the same held-file identity rules, and only that successful alignment may
    CAS the marker to `legacy-aligned`.

    The schema fence advances last, after legacy is `D1`-aligned. Once it is v3
    at generation `G+1`, the predecessor may write immediately. A crash at that
    point completes from durable `legacy-aligned` evidence plus that fence by
    metadata-only CAS to `complete`: post-fence replay verifies only the
    immutable marker, exact external `D1`, and either no recorded fence or v3 at
    the exact recorded `G+1` `schema_fence_generation`; it MUST NOT reopen,
    redigest, realign, or re-prove legacy tail/adjacency or require v4 or backup
    custody. The current relocated runtime remains fenced while v0.57 uses the
    legacy store, whose receipt chain may advance from `D1`. Recovery is
    descriptor-scoped: explicit
    offline `--adopt-state governance-store=vault` later proves a valid legacy
    descendant receipt chain anchored at recorded `D1`, re-externalizes governance
    only, preserves byte-identical every unrelated external family, and clears the
    marker only after exact migration proof. Generic lossy global-vault adoption
    is not this recovery path. Acceptance includes the real old-v3 binary making
    a receipt-bearing write, backup-restore parity, and failure injection at every
    marker, transaction, publication, receipt, alignment, and fence boundary.

## Explicitly out of scope

- Warm-up walk performance and warm hybrid latency (own packet; re-measure
  after isolation).
- Any change to refusal semantics, graph accounting, or #840's remaining
  write-amplification work.
- Multi-writer leases across machines (the external root is machine-local
  by construction; cross-machine coordination remains lease-spec territory).
- Unifying service/lease runtime state under `EXOMEM_STATE_ROOT`; the existing
  writer-lease precedence, lock identity and deployment contract remain intact.
- Replacing target-adjacent atomic publication with a cross-volume transaction
  protocol.

## Risks

- A consumer path missed by the sweep keeps writing in-vault → combine a
  source-described family inventory pin with a registry-driven constructor
  walk and doctor leftover scan; registry equality alone can be
  self-consistently incomplete.
- Moving publication scratch to the external root breaks same-volume atomic
  rename → placement tests assert target adjacency separately from external
  state.
- A public path field leaks usernames or checkout layout → privacy tests pin
  `/health` to path-free status while doctor owns the detailed path.
- Swallowing private-root setup failure exposes governance or voice state →
  fail closed and test the error path.
- Tests that assume in-vault state paths: fixtures already inject state
  roots or temp vaults; the seam must honor `EXOMEM_STATE_ROOT` so fixtures
  point it at the tmpdir (no test may write the real user state root).
