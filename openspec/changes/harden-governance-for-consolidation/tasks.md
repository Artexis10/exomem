## Progress Accounting

This checklist is an end-state acceptance ledger, not a percentage-complete meter. A
checkbox is complete only when its whole obligation is evidenced; several broad open
tasks therefore contain substantial merged foundations. The implementation baseline for
this reconciliation is `origin/main` at `1a7f30e1` (merged PR #778).

| Area | Merged implementation evidence | Remaining end-state obligation |
| --- | --- | --- |
| Per-scope disclosure | Conservative lattice, option meet, unconditional scrubber, and session-bound scope enforcement are merged. | Exact v3 grant migration/recovery closure in 1.7/1.10. |
| Non-Markdown membership | Descriptor binding, owner backfill, fail-closed propagation, and structured-read preflight are merged in #723, #724, and #727. | Final combined evidence run in 2.11. |
| Policy authority | Prospective snapshots and the immutable active policy/catalog tuple substrate are merged in #729 and #740. | Product writer/mirror integration, migration, recovery, and concurrency closure in 3.3-3.7 and 3.10-3.13. |
| Reserved state | Held filesystem substrate and the closed reserved-path monopoly are merged and complete. | Final whole-change review/verification only. |
| Authorization sessions | Credential verification, external custody, schema-v4 authority, transport binding, standalone attachment, and serving-membership verification are merged in #728, #732, #734, #737, #741, #772, #775, and #778. | Hosted membership publisher, complete move/restore and v3 migration/downmigration, hygiene, and combined lifecycle evidence in the remaining section 5 tasks. |
| Direct reads | Markdown projection and structured direct-read gates are merged in #730 and #731. | Complete the remaining L0-L6/never-enrolled matrix and combined regression evidence in 7.1/7.6. |
| Projected retrieval | Projection/measurement stores, preactivation, request-local authorization, lane reduction, and timing/release foundations are merged in #748-#753 and #771. | #774 deliberately reclosed serving until genuine hidden-state error, real pagination, exact-capacity, and active-model evidence satisfy 8.1-8.6, 8.9, 8.11, and 8.14-8.16. |
| Closeout | No consolidation workflow is exposed. | Sections 9-11: integration/migration gates, independent security recheck, independent verification, canonical sync, and archive. |

## 0. Evidence-Gated Prerequisite Bookkeeping

- [x] 0.1 Record the durable Wave 0 closeout evidence before bookkeeping: independent
  review, three findings fixed, same-reviewer recheck, 11/11 green CI, and merged PR
  #367 (`14ba1cc`) are pinned in archived task 6.6.
- [x] 0.2 Sync the shipped Wave 0 egress requirements into canonical
  `governance-kernel`/`release-gate` only after that evidence, and archive them at
  `openspec/changes/archive/2026-08-13-fix-governance-egress-defects/`.
- [x] 0.3 Confirm the merged Wave 1 default-deny requirements and completed independent
  review are canonical, then archive them at
  `openspec/changes/archive/2026-08-13-add-default-deny-scope-cap/` before authoring this
  delta against the post-archive contract.

## 1. Per-Scope Disclosure Lattice

- [x] 1.1 Add red tests in `tests/test_governance_decisions.py` proving a standing grant
  for scope A cannot lift overlapping default-deny scope B, in both scope/document
  orders; record the expected current item-wide-grant failure before implementation.
- [x] 1.2 Add red tests proving session grants carry exact reviewed scope IDs, a grant
  covering every matched scope raises each independently, and a legacy unscoped session
  grant contributes no ceiling.
- [x] 1.3 Add red tests for per-scope standing minima, default values, organisation caps,
  owner default-deny exemption, complete-decision purpose monotonicity, and final item
  minimum. Include equal-level declared/undeclared branches where only one contributes
  notice/constraint/abstract/bridge/release identity and assert no
  field can be added by declaring purpose.
- [x] 1.4 Add red overlap tests where equal ceilings have conflicting constraints,
  notices, abstracts, bridges, release identities, and strip sets; prove ambiguity/
  absence narrows or fails closed and rule-id order never
  selects a winner. Add property tests for associative, commutative, idempotent folding
  across every rule/scope/purpose permutation and parenthesization.
- [x] 1.5 Add red compiler tests replacing the current arbitrary-options fixtures: the
  closed registry accepts only bounded provenance-free string
  `notice`/`constraint`/`abstract`/`bridge`, boolean control `suspended`, and release
  `strip_provenance`; every `credential_scrubber` spelling/value (including legacy
  on/off and booleans), unknown keys,
  compiler-owned fields, custom YAML tags/containers, unsafe/oversized values, wrong
  types, and non-finite numbers are compile ERRORs.
- [x] 1.6 Add red mixed-scope, mixed-item, error, and governance/control-envelope tests
  proving the shared terminal secret/bearer scrubber is non-disableable under active
  governance. Assert legacy scrubber-off policy blocks compilation and the exact typed
  open/rotate issuance occurrence remains the only terminal exception.
- [ ] 1.7 Extend `tests/test_govern_memory_tool.py` and `tests/test_governance_store.py`
  with red persistence/drift tests for exact session-grant scope bindings and a
  monotonic migration that expires legacy rows unable to prove them.
- [x] 1.8 Implement the pure per-scope lattice and closed conservative option meet in
  `governance/decisions.py`, retaining deterministic explanations of each scope's
  standing, grant, cap, option, ambiguity, and final contribution; remove sorted-rule-id
  overwrite and equal-level declared-branch selection.
- [x] 1.9 Remove `credential_scrubber` from accepted policy options, add the owner
  migration finding for every legacy occurrence, and make terminal scrubber invocation
  unconditional across content/error/control dispatch; retain only the exact validated
  issuance exception.
- [ ] 1.10 Persist the reviewed scope set with session grants, return it from active-grant
  lookup, include it in composite digests/recovery, and make membership or policy drift
  invalidate rather than broaden it.
- [x] 1.11 Run the focused compiler/lattice/scrubber/store/tool tests and preserve the red-before-green
  output in the implementation evidence.

## 2. Fail-Closed Non-Markdown Membership

- [x] 2.1 Add red tests in `tests/test_governance_membership.py` for a missing companion
  under semantic-only project/tag/type/class scopes, proving the outcome is unresolved
  rather than the classified empty set.
- [x] 2.2 Add red tests for malformed, unreadable, stale, symlink-escaping, and
  artifact-mismatched companions; cover duplicate/relocated dataset cards and scene-frame
  parent/path/hash/timestamp mismatches, assert the same fail-closed outcome, and prove no
  cached open answer.
- [x] 2.3 Add red/passing distinction tests for path/ref-only policy, a path/ref exclusion
  that proves a semantic scope excluded, a positive path match plus an unresolved
  semantic sibling, and a valid explicitly empty companion. The empty fixture must carry
  `state: classified`, all four explicit empty semantic lists, and the complete class-
  specific immutable binding tuple; missing semantic keys/descriptor must stay unresolved.
- [x] 2.4 Add exact red legacy/backfill fixtures for `preserve.py` minimal media stubs,
  pending and completed media sidecars, ordinary binaries, dataset cards, and persisted
  scene frames. Require owner-authenticated receipt-first version-1 input with exact
  artifact/companion identities, complete explicit semantics, and class binding fields.
  Assert page tags/projects/type/classes are never inferred, pre-descriptor semantic-only
  classification is unresolved, exact retries are idempotent, and drift/crash/ambiguity
  leaves no partial descriptor or rewritten body/page metadata.
- [x] 2.5 Add red legacy scene-frame fixtures for finite, negative, non-finite, half-way,
  and out-of-range `frame_ts`; filename/index/parent agreement and disagreement; and
  canonical `int(round(binary64(frame_ts) * 1000))` ties-to-even conversion. Require
  exact bounded `frame_timestamp_ms` in `0..4_294_967_295` or atomic refusal.
- [x] 2.6 Add red integration coverage in `tests/test_governance_egress.py`,
  `tests/test_record_governance.py`, and media/dataset tests proving unresolved artifacts
  are L0/missing before counts, frames, rows, proposals, or grant-drift checks.
- [x] 2.7 Implement the closed companion locator/descriptor registry: sibling binary/media
  path/hash/size/class (+ media type/original name), unique dataset-card
  data-path/hash/size/format, and scene-frame frame/parent path+hash+size and canonical
  bounded `frame_timestamp_ms`, plus exact artifact
  `projects`/`tags`/`types`/`classes` string lists
  distinct from companion-page metadata. Make all reads immutable, canonical, regular,
  no-follow snapshots.
- [x] 2.8 Implement `backfill_companion` preview/commit through trusted local-owner
  context and receipt-first no-follow held-parent/descriptor-bound transaction. Validate
  the exact version-1 input, preserve all non-descriptor bytes, persist prior/target
  identities and owner/event evidence, and prohibit every metadata-inference path.
- [x] 2.9 Introduce the typed classified/unresolved membership result and update memo keys
  so companion and artifact immutable identities participate without adding index-time
  materialization.
- [x] 2.10 Wire direct media/frame/dataset reads, corpus walks, proposal membership, and
  active-session-grant revalidation to propagate unresolved membership; remove every
  fallback that converts it to `frozenset()`.
- [ ] 2.11 Run the membership, preserve/media-processing, scene-frame, query-dataset,
  egress, Records, and grant-drift focused tests with embeddings/media model extras
  disabled; retain red/green output for every legacy fixture class.

## 3. Conflict- And Fingerprint-Bound Prospective Compile

- [x] 3.1 Add red tests in `tests/test_governance_policy.py` for every supported conflict
  marker present before prospective compile and appearing/disappearing between its two
  probes; assert no reviewable target and no state creation.
- [x] 3.2 Add red tests for timestamp-preserving byte replacement, added/deleted policy
  files, pending-guard generation changes, symlink/non-regular policy paths, and mixed
  before/after document sets during prospective acquisition.
- [ ] 3.3 Add red `tests/test_govern_memory_tool.py` coverage proving proposals persist
  exact workspace byte map/source/conflict/guard identities, reviewed active policy/
  projector/catalog tuple, canonical target compiled bytes/fingerprint/schema versions,
  exact membership, and ready projection namespace; reject any tuple mismatch at commit.
- [ ] 3.4 Add red tests proving direct workspace create/edit/delete/conflict never changes
  active authority. Prove valid edits/deletions remain pending and deletion does not
  revoke, while missing/corrupt/conflicted workspace blocks warm/restart content. Barrier
  every edit before/after snapshot and tuple commit; assert the cooperatively fenced
  held-parent/descriptor-identity mirror refuses observed drift, while only the expected
  immutable target may activate. Direct OS-owner mutation is outside that guarantee.
- [ ] 3.5 Add deterministic SQLite barrier/crash tests for two commits sharing one
  predecessor and policy commit versus content create/edit/delete/companion publication:
  stage-before-tuple, tuple/receipt/namespace transaction, reader snapshot, response, and
  workspace mirror.
  Assert one full-tuple CAS winner, predecessor-or-target visibility,
  exact nonce recovery, append-only generations, and no stale policy/catalog projection.
- [ ] 3.6 Add red startup/migration/backup tests for current direct-source v3 to v4:
  ordinary v3 opener leaves exact v3 with no v4 DDL/DML; quiesced explicit migration;
  monotonic external `governance_enrolled` plus expected store id/epoch/digest; stable
  source/catalog recheck; atomic initial tuple; stopped/running deletion and corrupt/
  missing/aliased registry/workspace/store/tuple/policy/catalog/namespace fail-closed;
  WAL-consistent restore and no last-good/OPEN/historical fallback.
- [ ] 3.7 Add red `govern_memory` suspend/resume/undo matrices: semantic widening and
  narrowing; changed dependent scope/member identity/hash/set with deterministic expire/
  review state; exact policy generation + grant manifest + ready namespace identity;
  races against content create/edit/delete and companion publication; cooperatively fenced
  mirror observed-drift refusal/valid divergence/invalid partial state; and crash barriers before/after target
  preparation, receipt intent, narrowing overlay, tuple publication, external digest
  acknowledgement, response, and mirror. Assert predecessor-or-complete-target only,
  one tuple winner, no stale grant/catalog hybrid, and no semantic replay.
- [x] 3.8 Split the pure pinned-byte compiler from guarded live prospective acquisition;
  implement a before/read/after `AuthoringSnapshot` and return a bound
  `ProspectiveCompile` only for a stable conflict-free regular-file tree.
- [x] 3.9 Add append-only `compiled_policy_generations`, immutable catalog descriptors,
  and singleton `active_governance_tuple` to exact schema v4. Store ordered source and
  compiled bytes/fingerprints, compiler/projector versions, predecessor/event/receipt,
  projection namespace, immutable store id, epoch, and activation digest; enforce
  no-follow private store and immutable row constraints.
- [ ] 3.10 Implement one `BEGIN IMMEDIATE` publication transaction that inserts the
  complete target and CASes the full policy/projector/catalog tuple with receipt,
  activation epoch/digest, and ready namespace. Make policy commits CAS expected catalog,
  content/companion writers CAS expected policy, and readers pin one tuple; remove direct
  workspace loading and independent policy/catalog pointer linearization.
- [x] 3.11 Implement the separate cooperatively fenced, handle-relative no-follow
  workspace mirror with held-parent/descriptor-identity checks. Refuse observable drift,
  record exact mirror result and pending divergence, and allow an already-exact tuple
  transaction to stand; direct OS-owner mutation is outside the filesystem guarantee.
- [ ] 3.12 Restrict recovery/doctor/backup/rebuild/downmigration compiles to immutable
  generation/journal/receipt bytes. Implement irreversible registry enrollment, initial
  direct-source/catalog migration, exact active-source mirroring for v4→v3 under the
  cooperative fence, and external expected-tuple restore/rebuild CAS. Preserve enrollment
  history and treat a reviewed empty generation as governed, never OPEN.
- [ ] 3.13 Run policy, govern-tool, activation-store, suspend/resume/undo dependent-grant,
  recovery, receipt, conflict-copy, proposal concurrency, migration/rollback,
  backup/restore, and cross-process reader tests; retain red/green evidence for every
  barrier.

## 4. Reserved Administration Path Monopoly

Execution order is binding despite the retained task numbers. **PR A** has no closed
registry dependency: 4.4a writes the red native primitive/API fixtures, then 4.7 provides
and verifies that primitive, its runtime capability probes, and internal identity
publication. **PR B** starts only after PR A verification: 4.6 begins with its focused
red registry-construction/classifier cases and establishes the closed registry, 4.1
completes its inventory/invariant suite after 4.6, 4.4b adds the registry/leaf/lifecycle
race matrix, and 4.8-4.10 plus 4.2, 4.3, and 4.5 wire dispatcher, leaves,
and surface parity. 4.11 runs required combined verification. No public security claim is
made before both PRs and their combined verification are complete.
- [x] 4.2 Add red source/destination tests for create/file+directory, edit/observe/replace,
  append, move/copy, delete, recursive delete, trash, explicit recovery target,
  metadata-derived original recovery target, recursive recovery children, upload,
  download, export, and transfer for every registered internal family. For each newly
  named graph/review/lexical form, exercise its exact pre-create spelling, live name,
  transactional sibling, and stable pre-existing physical alias as both source and
  destination.
- [x] 4.3 Add red read/structured tests for get/fetch/list/browse/search, dataset,
  walk, `record_memory`, media/process/read/frame, audit/repair, export, and every
  `manage_memory_file` alias variant; assert reads hide and generic writes refuse before
  existence/count/parse/membership effects, including owner/L6 and non-Markdown routes.
  Pair absent/present fixtures for every graph-sync/floor/receipt descendant,
  review-state/temp, and lexical rebuild/quarantine member and require stable list/walk/
  search/get/download/dataset/export/transfer envelopes.
- [x] 4.4a **PR A:** Add red native primitive/API fixture tests, independent of the
  closed registry and public leaves: held no-follow parent acquisition, relative
  read/write/rename/link/unlink API behaviour, same-device versus cross-device result,
  destination-only copy publication, ordered per-entry saga records, stable identity
  publication under coordination, and runtime capability-probe/fallback-disable results.
  Cover POSIX dirfd/openat2 and Windows `NtCreateFile` RootDirectory-relative plus
  `NtSetInformationFile` fixture contracts.
- [x] 4.4b **PR B, after 4.6:** Add barrier-controlled, anchor-observable end-to-end
  registry/leaf/lifecycle TOCTOU tests: swap a checked parent to symlink/junction/reparse/
  bind alias; rename/exchange source or destination; add a hard link; change recovery
  metadata/recursive child; race cross-device copy publication; and race DB/WAL/SHM/
  journal/index staging, checkpoint, rename, deletion, and graph rebuild. Add barriers at
  graph checkpoint/floor/receipt pre-create, review-state temp create/replace, lexical
  rebuild-temp publish, and grouped main/WAL/SHM quarantine/restore while
  list/download/recovery/delete/link runs. Assert retained-anchor discrepancies fail
  closed within the cooperating boundary; test same-device leaf operations under
  coordination, cross-device move/trash/recovery refusal, destination-atomic copy only,
  and saga/recovery for recursive or multi-entry power loss rather than external same-UID
  zero-effect or all-or-none claims.
- [x] 4.5 Add red parity tests in `tests/test_command_surface_retry.py`,
  `tests/test_rest_registry.py`, `tests/test_hosted_private_routes.py`, and CLI coverage
  proving reads are missing and mutations return the same content-free reserved-path
  code across MCP, REST, Hosted, and CLI.
- [x] 4.6 Add focused red registry-construction/classifier tests, then implement one
  closed versioned internal-state descriptor registry plus pure logical classifier with
  separator, NFKC, casefold, prefix,
  ref, alias, and platform-name normalization plus one secure physical-target check that
  rejects symlink escape, compares retained stable filesystem identity for pre-existing
  hard-link/bind/physical aliases at protected acquisition, and refuses
  ambiguous/non-canonical or multiply linked reserved targets; fail closed only for drift
  observable against logical/catalogue/registry/identity anchors.
- [x] 4.1 **PR B, after 4.6:** Complete the red closed-registry inventory/invariant suite
  in `tests/test_reserved_admin_paths.py` covering `_Governance`, `_Consolidation`,
  governance/embeddings/CLIP/lexical/graph/claims/refs/deferred/media/idempotency SQLite
  names and every WAL/SHM/journal, JSON/lock/legacy spelling, graph rebuild temp, and
  `.authorization-projections/**`; explicitly include `.graph-sync.json`,
  `.graph-sync-floor.json`, `.graph-commit-receipts/**`, `.review-state.json`, exact
  `..review-state.json.[a-z0-9_]{8}.tmp`, exact lexical rebuild temp family, and grouped
  main/WAL/SHM lexical quarantine forms. Derive an independent expected inventory from
  every current private-state owner/path factory—not hosted portability—and require one
  descriptor each. Keep the matrix at the Exomem/cooperating-subsystem boundary: static
  logical reservation before existence, plus protected acquisition of stable pre-existing
  symlink/reparse/hardlink/physical aliases. Also cover case/NFKC/separators/prefix/dot/
  drive/UNC/ADS, short names, refs, and managed aliases; do not claim universal detection
  or zero effect for direct OS-vault-owner filesystem/block access.
- [x] 4.7 **PR A:** Provide and verify the shared native held-handle primitive used later
  by every leaf and internal-store lifecycle: publish SQLite primary/WAL/SHM stable
  identities before releasing cooperative coordination, not before filesystem
  reachability; hold no-follow vault/parent traversal through primitive operations,
  relative create/read/write/rename/link/unlink, same-device dual-parent rename/trash/
  recovery, held-source/destination-atomic copy only, and handle-enumerated saga records
  for recursive operations. Use POSIX
  `openat2`/equivalent dirfd primitives and Windows `NtCreateFile` RootDirectory-relative
  plus `NtSetInformationFile` semantics. Add a runtime actual-filesystem capability probe
  proving relative handles, no-follow/reparse behaviour, and final identity checks;
  disable/refuse an unsupported route rather than using a path fallback. PR B wires this
  primitive into descriptor-bound reads/writes for every leaf and lifecycle.
- [x] 4.8 Extend registry metadata with owning subsystem and source/destination/recursive/recovery/dataset/media
  path roles, and add startup tests that enumerate every command and finite selector and
  fail on an unclassified path/ref parameter.
- [x] 4.9 Invoke the classifier at the shared dispatcher before existence checks,
  parsing, counting, planning, or leaf dispatch, while requiring the handle transaction
  as leaf authorization rather than treating preflight as race defense.
- [x] 4.10 Give `govern_memory` a private non-serializable `_Governance` authority; reserve
  `_Consolidation` for the future owning command with no public bypass flag and no
  fallback owner until it exists; give each registered database/index only its named
  subsystem token; owning commands still use safe handle traversal and no generic bypass.
- [x] 4.11 Run the reserved-path and active race matrices on Linux and the Windows-specific
  windows-latest NTFS junction/reparse/hard-link/8.3/rename-disposition/fallback-disable
  CI job; wire it into the required combined release gate. Prove enumeration/download/
  export/dataset/transfer never disclose internal state through Exomem, same-device
  coordinated rename/trash/recovery and destination-atomic copy meet their contracts,
  cross-device move/trash/recovery refuse, and no refused Exomem case creates a file,
  directory, receipt, marker, sidecar, or index row. Complete this only after PR A
  (primitives, capability probes, internal identity publication) and PR B (registry,
  dispatcher, all leaves, surface parity) have landed.

## 5. Durable Authorization-Session Capability Core

- [x] 5.1 Add red pure tests in `tests/test_authorization_session_binding.py` for exact
  70-byte `as1.<22-char canonical b64url 16-byte locator>.<43-char canonical b64url
  32-byte secret>` issuance, bounded decode/re-encode parser, parser-derived scrubber
  matcher, constant-time verifier success/
  failure, and exact binding of domain, locator, stable session id, credential generation,
  canonical principal, issuer family, external cell/logical-vault/keyring ids, and expiry.
  Cover padding/whitespace/alternate alphabet/wrong length/noncanonical bits/duplicates,
  malformed/expired/closed, cross-principal/cross-issuer/cross-cell/cross-vault,
  unregistered copy, unknown-key equivalence, and accepted-parser⊆scrubber property.
- [x] 5.2 Add red custody tests for `EXOMEM_AUTH_SESSION_KEYRING_FILE` and
  `EXOMEM_AUTH_SESSION_CONTROL_FILE`: absent, symlink/non-regular, oversized, bad
  version/id/key length/time/signature, identity mismatch, permissive POSIX mode or
  Windows ACL, policy/request/CLI/vault-provided values, and first-use generation all
  fail closed. Prove rows store external ids + key id + keyed verifier, never key/bearer.
- [ ] 5.3 Add red authenticated standalone/hosted provisioning tests for atomic external
  registration of cell/logical-vault/keyring/storage/owner identity plus monotonic
  `governance_enrolled` and expected activation store id/epoch/digest. Prove false only
  after a trusted negative scan for governance workspace/activation state, irreversible
  false→true before store/workspace creation,
  and stopped/warm deletion/corruption/mismatch BLOCKED. Cover concurrent
  vault+sidecar+keyring/control copies, copied external files without registry ownership,
  exact offline restore, clone with new ids, and detach-ack/attachment-epoch move; no
  colliding pair may serve simultaneously or synthesize identity.
- [x] 5.4 Add red lifecycle tests for exact typed `issued_credential` open/rotate,
  status, close, replay of the old bearer, restart/resume, and two v4 processes/replicas.
  Add malformed/extra-copy/raised issuance tests proving the terminal scrubber exception
  activates only after exact response validation and otherwise scrubs/refuses.
- [ ] 5.5 Add red hygiene tests that scan request copies, sidecar rows, receipts, journals,
  token claims, logs, metrics, traces, error/remediation strings, repr/debug output,
  idempotency/control-plane rows, and corpus projection for the exact raw bearer and find zero copies outside the
  exact typed successful issue field/protected request input; scan retry/validation/
  exception copies and assert issuance text retrieved from content stays inert.
- [ ] 5.6 Freeze an exact current schema-v3 fixture from `store.SCHEMA_USER_VERSION == 3`
  containing legacy handles, grants, purposes, tokens, receipts, and live recovery
  journals/direct-source policy. Prove every ordinary v3/v4-capable opener leaves v3 with
  no v4 DDL/DML; add red explicit v3→v4 session + immutable policy/catalog/tuple migration
  crash tests, irreversible enrollment, and unknown >v4 refusal.
- [ ] 5.7 Add actual rollback/rollout tests: run the real pre-change v3 binary against an
  isolated v4 copy and record exact startup/read/authoring behavior without permitting
  DDL/DML; prove the deployment schema/lease fence bars it from a live v4 cell even if a
  path does not self-refuse. Restore the v3 snapshot and test offline v4→v3 closure/
  expiry/schema plus exact pointed-source workspace parity before booting v3. Do not
  permit or claim mixed v3/v4 service.
- [x] 5.8 Add red authoritative serving-membership-epoch tests with two+ replicas:
  authenticated current/stale attestations, active/accepted intersections, unreachable
  member blocking, old/new key overlap, issuance switch, max-TTL+skew/live-row drain,
  explicit `SERVING -> DRAINING`, no-in-flight acknowledgement, epoch advance/removal,
  and current-epoch rejoin. Silence/gossip MUST NOT remove a replica from the intersection.
- [x] 5.9 Add red same-principal cross-session tests: tokens, grants, purpose, revoke, and
  close from session A are rejected or isolated in session B; missing/unresolved session
  refuses without consuming or writing anything.
- [ ] 5.10 Implement transactional schema v4
  `governance_authorization_sessions` with internal session id, keyed locator digest/
  verifier, verifier key id, credential generation, principal, issuer family, external
  cell/logical-vault/keyring ids, status, and lifecycle timestamps only, plus the shared
  immutable policy/catalog and active-tuple schema. Conservatively expire v3 arbitrary-handle/
  unscoped authority and add the explicit offline downmigration tool; drain v3 replicas
  before first migration.
- [ ] 5.11 Implement strict external keyring/control loading, authenticated owner/hosted
  provisioning, host/control-plane registry attachment with monotonic enrollment and
  activation-store tuple parity, copy collision, exclusive move/restore/clone rules, and
  no automatic key/cell identity or OPEN inference.
- [ ] 5.12 Integrate the serving-membership epoch with the existing deployment control
  plane/readiness surface: provisioner/cell control owns the Hosted record,
  `hosted_runtime.control_plane_readiness()` verifies/exposes content-free readiness, and
  the standalone host registry uses the same schema. Implement authenticated per-replica
  attestation, admitted intersection, explicit drain/ack/epoch advance and current-epoch
  rejoin; gate issuance/resumption on cell identity and epoch, never observed liveness.
- [x] 5.13 Implement domain-separated verifier construction, bounded lookup, constant-time
  comparison, active/accepted key rotation, and row/presented-credential equality to the
  external cell/logical-vault/keyring identity.
- [x] 5.14 Implement server-issued open/status/rotate/close with cryptographic randomness,
  typed issuance, keyed at-rest verifier, bounded TTL, atomic rotation, and close-time
  revocation of purpose, grants, and unconsumed tokens.
- [x] 5.15 Change escalation tokens, session grants, declarations, and revoke to bind the
  stable internal session ID plus principal/issuer/audience/purpose/expiry and exact
  path/fingerprint/scope bounds, never the raw bearer or a caller-selected handle.
- [x] 5.16 Add the generated `govern_memory(operation="session")` selector and explicit
  open/status/rotate/close argument validation; treat legacy handles only as bounded
  echoes after capability verification and never as first-use authority.
- [ ] 5.17 Run session, keyring/control/registry, membership epoch, migration/downmigration,
  actual old-binary probe/fence, token, store, governance-tool, receipt, and crash-
  recovery suites and preserve exact red-before-green commands/results for standalone,
  hosted, and both supported OS custody paths.

## 6. Real MCP REST Hosted And CLI Principal Binding

- [x] 6.1 Add red MCP tests proving stateless HTTP cannot gain session authority from
  repeated/changed `Mcp-Session-Id`, `Context.session_id`, request UUID, connection, or
  arbitrary session id; stable stdio/SSE context is supplementary only. Assert the only
  public MCP bearer placeholder is `authorization_session_credential`.
- [x] 6.2 Add an actual-wire stateless MCP reconnect suite: open and grant, disconnect,
  route the bearer to another replica, then exercise the registered `ask_memory` search,
  `read_memory` direct page, Records read/query, `query_dataset`, and frame-bearing
  `read_media` routes.
  For every route compare valid bearer, absent bearer, transport-session-only, invalid,
  cross-principal, and same-principal-cross-session credentials and scan all wire/server
  copies for the raw bearer.
- [x] 6.3 Add installed-FastMCP actual-wire red tests that send valid/invalid/duplicate/
  non-string bearer values at exact raw JSON-RPC
  `params.arguments.authorization_session_credential` alongside malformed ordinary
  arguments. Require credential refusal before FastMCP logging/`FunctionTool`/Pydantic,
  sanitized wrapper arguments, and zero bearer copies in access/validation/exception/
  retry/trace/debug logs. Add JSON-RPC batch matrices containing any `tools/call` with
  A+absent, A+B, invalid+valid, duplicate carrier key/value, notifications/non-tool calls,
  and reorderings; require one atomic batch refusal and zero executed elements/effects.
- [x] 6.4 Add red REST/Hosted tests for sensitive
  `X-Exomem-Authorization-Session`, distinct from service `Authorization`, after trusted
  access/gateway principal resolution. Reject body/query bearer carriers and caller
  principal/audience/issuer/cell/internal-session fields; scan raw ASGI/log/error copies.
- [x] 6.5 Add red CLI/in-process tests proving the trusted local-owner adapter binds
  explicitly and only `--authorization-session-fd <fd|->` reads one bounded bearer from
  a protected descriptor/stdin. Reject environment and literal argv bearers, clear
  buffers, prevent principal selection, and fail an unbound library path closed.
- [x] 6.6 Add registry-fidelity red tests enumerating every command, legacy leaf, finite
  selector, retrieve/inject hook, and content-bearing writer result into exactly one
  credential row: open-forbidden; lifecycle/session-authoring-required; self/content-
  optional; owner/standing-not-authorizing. New/unclassified routes must fail startup.
- [x] 6.7 Add order-of-operations red tests that submit an invalid credential with
  malformed/cacheable/idempotent inputs on every surface and assert credential refusal
  happens before coercion/validation detail, cache key/lookup, idempotency lookup,
  membership/decision, receipt allocation, or leaf effect.
- [x] 6.8 Introduce one immutable verified authorization context at the shared dispatcher;
  implement bounded raw pre-framework extraction/redaction for exact carriers, resolve
  trusted per-surface principal/issuer and external cell, then verify capability before
  all ordinary request work.
- [x] 6.9 Implement MCP raw JSON-RPC/ASGI (plus stdio/SSE) interception that consumes and
  removes the placeholder before FastMCP logging, `FunctionTool`, or Pydantic; pass only
  trusted context and sanitized arguments to generated wrappers/leaves. Bounded-parse and
  sanitize batches at this boundary, then reject the entire batch before dispatch whenever
  any element is `tools/call`; never share request context across elements.
- [x] 6.10 Wire REST/Hosted sensitive-header middleware and CLI protected-fd reader, then
  MCP, retrieve/inject hooks, hand-registered residuals,
  and writer read/mutation paths through that resolver; reject caller identity fields at
  admission and never downgrade a presented-invalid optional credential to standing-only.
- [x] 6.11 Redact protected session credentials before logging, validation rendering,
  tracing, retry/idempotency material, and exception envelopes on every surface.
- [x] 6.12 Regenerate MCP/OpenAPI/CLI/capability artifacts intentionally and update
  schema-fidelity tests for the consumed MCP placeholder, REST/Hosted protected header,
  CLI descriptor option, and session lifecycle; assert no bearer body/query/env/literal-
  argv or public authoritative principal/session parameter appears.
- [ ] 6.13 Run real generated-surface parity plus installed-wheel E2E covering typed open,
  grant/declare, every content family, reconnect, restart, mixed-key v4 replica routing,
  rotate, cross-session refusal, close, invalid+malformed FastMCP precedence, and complete
  bearer-copy scrubbing.

## 7. Governed Raw Direct-Read Projection

- [ ] 7.1 Add red `tests/test_get_payload.py` cases at L0-L6 for default,
  `frontmatter_only`, `include_raw=false`, and `include_raw=true`; assert exact bytes/hash
  only at L6, identical true/false projection at L1-L5, and same-input present-L0 versus
  absent byte equality. Add secret-free and secret/canonical-bearer L6 fixtures in both
  governed and registry-proven never-enrolled modes: exact raw only when scrub-safe;
  otherwise deterministic content-free `SECRET_BLOCKED`, never redacted-as-raw, while
  full-raw hash and stale-edit semantics remain unchanged.
- [x] 7.2 Add red provenance fixtures covering forward/reverse citations, sources,
  history, links, relation/supersession, and parent-media fields; prove no raw option or
  direct-read variant restores them below L6.
- [x] 7.3 Add red Tier-2 direct-content coverage for dataset rows, media bytes,
  video frames, Records values/reductions, and companion metadata below L6, including
  unresolved non-Markdown membership and every registered internal-state path. Assert each
  structured direct route is byte-identical to missing at L0-L5—no partial row,
  aggregate/profile, record, byte, or pixel projection exists in this change.
- [x] 7.4 Add red paired tests proving a valid bound companion may be discovered/read at its
  lower Markdown projection through recall/get while direct dataset/Records/media/frame
  routes stay missing and never silently substitute the companion.
- [x] 7.5 Refactor direct get to decide and project one immutable file snapshot before
  optional raw assembly; add `content` only on the L6 branch, keep internal hash use
  separate from public hash projection, run the shared terminal parser before emission,
  omit/refuse secret-bearing raw deterministically, and enforce L6-or-missing for every
  structured direct representation registered by 7.3.
- [ ] 7.6 Preserve registry-proven never-enrolled/L6 default shape, byte-exact scrub-safe
  opt-in raw content, full-raw `content_hash`, and `edit(expected_hash)` stale-write
  semantics; update lower projection code/docs so excerpts/bridge abstractions are
  Markdown-only and run payload, egress, media, dataset, edit, and postfilter tests.

## 8. Counterfactual Retrieval Rank Graph Count Error And Timing Closure

- [ ] 8.1 Add a deterministic red paired-fixture suite in
  `tests/test_governance_oracle_closure.py` that serializes the exact same request with
  each L0 artifact present versus physically absent and compares the complete canonical
  governed envelope, not selected fields. Register one transport normalizer that may
  remove only echoed JSON-RPC id, HTTP Date/outer trace headers, and framing; application
  code/message/remediation/data, request ids, timings, warnings, and diagnostics remain.
- [ ] 8.2 In that suite, cover high/middle/low hidden ranks, keyword/BM25 fusion,
  raw-lane caps greater than visible top-k, projection-only query terms, projected-corpus
  DF/IDF, vector projection-only/raw-only relevance, reranking seams, top-k displacement,
  exhausted/non-exhausted sources, every pagination boundary, order, cursors, totals,
  facets, ambiguity counts, degraded diagnostics, and caller-visible candidate caps.
- [ ] 8.3 Add red CLIP/non-text pairs where the highest hidden pixel/keyframe match would
  consume the cap. Assert authorization occurs inside CLIP before its cap, L6 visible
  visual top-k is exact, and L1-L5 media participates only through its authorized textual
  companion projection or is excluded from the binary lane. Bind each image to one
  untimestamped sample and each video to one through forty strictly timestamp-ordered
  samples, returning the parent once with its earliest best `frame_timestamp_ms`.
- [ ] 8.4 Add red graph pairs where hidden vertices/edges change in-degree, out-degree,
  reachability, shortest paths, relation matches, seed expansion, graph-assisted fusion,
  and pagination; assert the visible graph/order is identical to physical absence.
- [ ] 8.5 Add red error pairs for hidden malformed/stale/index-missing items, duplicate
  identifiers, ambiguous refs, parser failures, and candidate-safety boundaries; assert
  identical success/error code, text, shape, count, and remediation when hidden versus
  absent.
- [ ] 8.6 Add red namespace tests proving its key is exactly `(policy_fingerprint,
  projector_schema_version, catalog_generation)`, immutable content identity is a row
  key, and extractor/model versions are measurement subkeys. Cover initial warming,
  complete build before active-tuple activation, atomic next-catalog generation, policy/
  projector rebuild, policy-vs-create/edit/delete/companion CAS races, reader single-tuple
  snapshot, stale-projection refusal, model-lane invalidation, duplicate/id mismatch/
  incomplete refusal, cursor pinning, exact-tuple GC, and no raw/old-policy fallback.
- [x] 8.7 Add red finite-variant fixtures enumerating unique outputs reachable from
  compiled audience/purpose/scope/grant-level domains. Verify RFC 8785 JCS/domain-hashed
  variant ids, L0 absence, fixed L1 notice/L2 constraint/L3 abstract/L4 bridge abstraction/
  L5 post-strip `_excerpt_of`/L6 permitted-full rows, deduplication, and hard per-item cap
  256 with activation refusal at 257 rather than drop/lazy/query generation.
- [x] 8.8 Add red L5 lexical/vector/rerank/snippet tests for whitespace-normalized,
  bounded first-600-code-point whole-token `_excerpt_of`: one variant/model vector,
  query independence, no acquisition by later hidden terms, no query-centered hidden
  snippet, and correct projection-only top-k.
- [ ] 8.9 Check in repository-owned constants `MAX_HIDDEN_CORPUS_WIRE_DELTA_MS=25`, ratio
  `0.10`, catalog items `16_384`, searchable bytes/item `1_048_576`, and graph edges
  `262_144`; make the release manifest lower-only. Add a red actual-wire harness with at
  least 200 predeclared randomized/interleaved samples at zero/one/exact capacity across
  every lane and 99% bootstrap upper bounds for absolute median/p95 differences.
- [x] 8.10 Add red anti-waiver tests: reject manifest/env/operator ceiling increases,
  capacity/lane reduction, scheduler-tolerance subtraction, post-observation padding,
  and missed fixed deadline. Require both bounds simultaneously within manifest, 25 ms,
  and 10% of physically-absent p95.
- [ ] 8.11 Implement principal-free canonical fixed projection records and lexical/vector/CLIP/
  graph measurement rows plus a request-local membership/decision authorization map that
  selects exactly one hashed variant or L0 per catalog artifact. Enumerate/deduplicate
  finite reachable outputs, enforce 256, and keep principal, purpose, session, grant, and
  request decision out of persistent keys/hot caches. Publish policy/projector/catalog
  only through the one active-tuple CAS: policy commits expect catalog; content/companion
  commits expect policy; readers never sample component pointers separately. For CLIP
  successors, verify the complete active family, carry only content-identical image/video
  rows, require target-item/content-hash-bound replacements for changed media, exclude
  `parent_media` frame companions from duplicate binary ownership, and publish the target
  namespace plus complete vector/CLIP roots atomically. Connect live scene sampling and
  bulk backfill to that successor with the already-computed canonical timestamp/vector
  tuple, guarded parent-sidecar binding, and no second model pass. For graph successors,
  verify exactly one active row per variant, emit empty lower-variant rows, carry only
  content-identical L6 rows, require target-item/content-hash-bound replacements for
  changed L6 sources, reject stale outside-catalog targets, and publish the complete graph
  root atomically. Validate conservative producer replacements for affected lower-only
  items, discard their edge payload, and emit only empty lower-variant rows. For
  existing-page semantic writes, semantic creations, semantic moves, semantic trash
  recoveries, and semantic file/directory trash, derive replacements from the freshest
  validated detached before-corpus
  carried into the mutation boundary, the move or recovery's exact detached after-corpus
  when applicable, and the exact guarded planned-write overlay; include indirect
  path/title-resolution, removed-target, and reverse-relation source changes. Retained-
  corpus paths never walk the vault again; trash builds one lazy before-corpus only for an
  active graph family before canonical mutation. No producer reopens the live graph.
  Apply the same lazy planned-Markdown contract to Evidence preservation, machine-owned
  media-sidecar updates, and scene-frame companion creation, co-publishing CLIP when
  applicable and doing no graph work for open or lexical-only tuples.
  Connect every other live graph producer to those replacements before checking
  this task; keep any unsupported required family blocked.
- [x] 8.12 Implement BM25 posting intersection before cap, projected-corpus DF/IDF and
  exact top-k; exact filtered projected-vector top-k with visible-lane warming/disable;
  projected-only reranking before final top-k; and L6-only pre-cap CLIP authorization.
- [x] 8.13 Authorize graph vertices and edges before expansion and recompute all public
  graph reductions over the projected graph; absorb errors belonging only to L0 state as
  absence.
- [ ] 8.14 Move fusion, final sort/top-k, pagination/cursor creation, counts/facets,
  ambiguity, diagnostics, and error reduction after complete projected lane acquisition;
  add cross-principal hot-cache reuse tests proving decisions/order stay request-local.
  Governed find continuation SHALL use the exact bounded `pc1` visible-snapshot digest
  and retained-runtime registry specified in `release-gate`; add red first/next/exhausted,
  hidden-only catalog drift, authority drift, expiry/cap/restart, replay, cross-binding,
  and generated-surface parity tests before enabling the release fence.
- [ ] 8.15 Implement stable payload timing suppression and fixed repository-registered
  public-request deadline/padding classes; fail if either bound exceeds any ceiling or
  maximum capacity cannot complete. Do not claim cryptographic constant time or pass by
  deleting timing fields; keep server-only aggregates content- and bearer-free.
- [x] 8.15a Register the exact `vectors-cpu-torch-v1` model/device/hard-off tuple and its
  distinct single-threaded 1,000/1,500 ms completion class; bind serving to the exact
  active vector
  measurement family, refuse mixed/override configurations, and cap only vector-model
  query input with the fixed 600-code-point whole-token projection.
- [ ] 8.16 Run the full no-model counterfactual and exact-capacity actual-wire suites, then optional live
  embedding/reranker and CLIP lanes behind their existing soft-fail/marker gates; no
  optional-model absence may skip keyword/graph or the declared timing security oracle.
- [ ] 8.16a Check in and pass the separate `vectors-cpu-torch-v1` 12-route actual-wire
  manifest/matrix with 200 hidden-present and 200 physically-absent REST observations per
  route at zero/one/exact capacity. Keep reranker, CLIP, GPU, ONNX, and mixed profiles
  closed pending their own evidence.

## 9. Integration Migration And Regression Gates

- [ ] 9.1 Add an overlap fixture representing personal plus delegated compartments with
  one dual-member Markdown page, one non-Markdown artifact, standing/session grants, and
  conflicting options; exercise get, recall, graph, Records, dataset, media, and
  governance inspection end to end.
- [ ] 9.2 Add migration tests from every supported governance sidecar version, including
  exact current v3 active legacy grants/purposes/tokens and v4 authorization sessions;
  prove ordinary open leaves v3 unchanged, explicit v3→v4 atomically initializes
  bearer-free sessions plus immutable policy/catalog/active tuple from stable direct-
  source YAML/catalog, enrollment is monotonic and deletion/corruption stays BLOCKED,
  mixed v3/v4 service is refused, legacy scrubber options block until owner migration,
  external registry/key/cell move/restore rules are conservative, and snapshot/offline
  v4→v3 closes session authority and mirrors the tuple's exact source before the real v3
  binary starts without erasing enrollment history.
- [ ] 9.3 Update generic capability/help documentation and the hand-authored generic skill
  scaffold only where the public session lifecycle or reserved-path remediation must be
  discoverable; keep all examples generic and pass the scaffold leak gate.
- [ ] 9.4 Run focused governance coverage with model extras disabled:
  `uv run python -m pytest -q tests/test_governance_decisions.py tests/test_governance_membership.py tests/test_governance_policy.py tests/test_governance_store.py tests/test_govern_memory_tool.py tests/test_authorization_session_binding.py tests/test_reserved_admin_paths.py tests/test_get_payload.py tests/test_governance_oracle_closure.py`.
- [ ] 9.5 Run surface, egress, Records, and media coverage:
  `uv run python -m pytest -q tests/test_governance_egress.py tests/test_governance_postfilter.py tests/test_governance_principal.py tests/test_governance_tokens.py tests/test_command_surface_retry.py tests/test_rest_api.py tests/test_rest_registry.py tests/test_hosted_private_routes.py tests/test_record_governance.py tests/test_media_processing.py tests/test_media_deletion_propagation.py`.
- [ ] 9.6 Run governance overhead and retrieval latency gates, including
  `tests/test_governance_overhead.py`, `tests/test_latency_gate.py`, and
  `scripts/semantic_write_latency.py --check`, plus the checked actual-wire hidden-corpus
  distribution gate; explain and fix any regression/security differential instead of
  weakening the ceilings or deleting timing fields.
- [ ] 9.7 Run `uvx ruff check`, `git diff --check`, `uv lock --check`,
  `tests/test_scaffold_no_leak.py`, schema-fidelity/golden checks, and public-artifact
  validation; regenerate only artifacts intentionally changed by this contract.
- [ ] 9.8 Run the lean full suite with embeddings and media extraction disabled, then the
  installed-wheel product E2E and optional embeddings-marked retrieval suite on an
  equipped host; retain exact commands, versions, counts, and results as durable
  evidence.
- [ ] 9.9 Run `openspec validate harden-governance-for-consolidation --strict` after the
  implementation diff and tests agree with every scenario.

## 10. Independent Security Review And Recheck

- [ ] 10.1 Have an independent security reviewer inspect the exact implementation diff
  against this proposal/design/spec set, explicitly attacking grant crossover, option
  last-write-wins/equal-purpose widening/meet laws and retired scrubber bypass, raw/
  provenance and structured-direct bypass, exact namespace/finite fixed variants/L5/
  candidate caps/IDF/vector/rerank/CLIP acquisition, immutable generation/tuple/mirror/
  recovery races and single policy/projector/catalog tuple CAS, companion locator/owner-
  input/backfill/frame rounding ambiguity, closed internal-state registry plus active
  owner inventory and graph-sync/floor/receipt, review-state/temp, lexical rebuild/
  quarantine parent/rename/link/reparse/bind/pre-create/checkpoint races, suspend/resume/
  undo dependent-grant manifests/direction/mirror/crash behavior, pre-FastMCP raw extraction,
  atomic tool-call batch refusal and caller-selected identity, exact bearer grammar/
  parser-derived scrubbing, typed issuance/bearer hygiene, L6 raw-vs-scrubber and L4/L5
  projector boundaries, external cell monotonic enrollment/copy/move/restore, serving-
  membership epoch/drain/rejoin/key rotation, v3-v4
  rollback, cross-session replay, canonical-envelope boundaries, and non-waivable exact-
  capacity actual-wire rank/graph/count/error/timing displacement.
- [ ] 10.2 Record every finding durably with severity and exact file/test evidence; for
  each accepted finding, add a reproducing red test before the smallest implementation
  fix and rerun the focused plus regression gates.
- [ ] 10.3 Require the same independent reviewer to recheck every finding against the
  amended exact diff and record explicit closure or a remaining blocker; no self-review
  or green CI substitutes for this gate.

## 11. Independent Verification And Evidence-Gated Closeout

- [ ] 11.1 Have a separate independent verifier run the focused suites, lean/full suites,
  latency/overhead plus normative 25ms/10%-ratio exact-capacity actual-wire gates, lint/
  privacy/schema and direct-source↔immutable-tuple v3↔v4 migration/downmigration/old-
  binary checks, external-enrollment stopped/warm deletion/corruption, internal-state
  registry disclosure/mutation races including graph/review/lexical temp/quarantine
  families, active policy/projector/catalog and suspend/resume/undo dependent-grant tuple
  races, L6
  raw scrub-safe/secret fixtures, atomic MCP batch refusal, and strict OpenSpec validation from a clean environment; record exact
  outputs and identify any skipped optional lane.
- [ ] 11.2 Have that verifier exercise real generated MCP stateless HTTP, REST, Hosted,
  and CLI flows across exact protected carriers, invalid+malformed installed-FastMCP
  precedence/log scans, typed issuance, reconnect/restart/mixed-key epoch replica routing,
  external cell copy collision/move/restore, find/get/Records/dataset/media/frame binding,
  owner-reviewed companion backfill, active tuple/mirror and registered internal-state
  races plus suspend/resume/undo mirror/crash recovery, exact bearer grammar/parser-derived
  scrubbing, L4 abstraction and L5 excerpt,
  JSON-RPC batch zero-effects, plus
  same-input hidden-versus-absent matrices; require canonical-envelope byte equality and
  measured latency bounds where specified.
- [ ] 11.3 Confirm no consolidation workflow is exposed until the prerequisite diff and
  migrations are deployed, the review/recheck is closed, and independent verification is
  green.
- [ ] 11.4 Only after tasks 10.1-11.3 have durable evidence, sync all six delta specs into
  the canonical capability specs and validate the canonical post-sync contract; do not
  edit canonical specs earlier to make validation pass.
- [ ] 11.5 Only after implementation merge evidence and canonical sync, archive
  `harden-governance-for-consolidation`, run strict validation over the archive/canonical
  state, and record the merge revision, review record, verifier record, and complete CI
  result in the archive closeout.
