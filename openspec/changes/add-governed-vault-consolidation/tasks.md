## 0. Prerequisite and red-first evidence gate

- [x] 0.1 Before touching consolidation product code, verify that
  `harden-governance-for-consolidation` is merged, its six delta capabilities are
  canonical, its sidecar migrations are deployed, its independent security
  review/recheck is closed, and its independent verifier evidence is green. Run
  `openspec validate harden-governance-for-consolidation --strict` against the
  merged tree and stop if any prerequisite is only specified rather than shipped.
- [x] 0.2 Record immutable pre-change baselines for the MCP schema fixture,
  OpenAPI operations, capability document, bootstrap ordering/size, Hosted v1-v4
  command membership and descriptor hashes, generated v1-v4 plugin/manifest
  fixtures, deployment locks, client contracts, and registered promotion
  evidence. These hashes are the negative assertion that consolidation adds v5
  without widening or auto-promoting v1-v4.
- [x] 0.3 Create a red-first evidence log for this change. For every test task
  below, run the named test before implementation, record its exact assertion
  failure (not collection/import failure), then rerun it green after the smallest
  implementation slice. A test introduced already green because it does not
  reach the missing invariant does not satisfy the task.
- [ ] 0.4 Keep the real operator vaults out of all implementation and release
  tests. Use only generated temporary vaults and explicitly clone-bound fixtures;
  capability completion SHALL NOT be treated as rehearsal, cutover, or source
  retirement approval.

## 1. Durable cell identity and authenticated portable intake

- [x] 1.1 Add red tests in `tests/test_consolidation_cell_identity.py` for
  standalone local and Hosted private identity records containing generated
  logical `vault_id`, generated `installation_id`, authenticated root binding,
  monotonic installation generation/fence, stable filesystem/root identity,
  version, and machine/cell key id. Store the adoption census only as immutable
  provenance; do not use it as live identity equality. Reject caller-supplied
  ids, roots, keys, signatures, trust roots, unknown
  fields/versions, symlinks, hard-link aliases, wrong owner/mode, missing machine
  key, malformed authentication, copied root/binding, and registry/fence mismatch
  before export or run creation. Add a Hosted regression proving typed routing
  `cell_id`, logical `vault_id`, and `installation_id` are independent and
  `cell_id == vault_id` fails before readiness/owner-context construction.
- [x] 1.2 Add red legacy-adoption tests proving only an authenticated local owner
  under exclusive lifetime/writer authority can adopt an unbound vault; adoption
  generates rather than accepts both ids, binds the stable root/filesystem and
  installation registry atomically, records its one-time census as provenance,
  is idempotent for the same identity, and leaves no partial
  marker/registry claim on every caught and abrupt failure seam.
- [ ] 1.3 Add red move/copy/collision tests. An owner-authorized same-installation
  move/rebind that proves the prior record and stable source root preserves logical and
  installation ids; copying the identity state to a second root, simultaneous
  root claims, reused installation ids, cross-machine record
  without its machine trust, and request-supplied replacement ids all fail closed.
  Separately prove adopt -> legitimate ordinary write -> quiesce/export succeeds
  with a newly signed current census and no identity-record re-MAC or equality to
  the adoption census.
- [ ] 1.4 Add red clone/failover distinctions. An explicit rehearsal clone receives
  new active logical and installation ids and immutable `clone_of_vault_id`,
  `clone_of_installation_id`, and `clone_of_snapshot_digest`; copying the binding
  is refused. A portability failover restore may preserve logical id under its
  existing contract but cannot claim rehearsal-clone semantics. Real start
  rejects equal source/destination logical or installation ids; rehearsal start
  requires distinct active clone ids/installations and approved distinct clone-of
  lineages. For logical-id-preserving failover, require
  `vault-identity-transfer/v1`: target-generated installation/challenge and N+1
  generation, authenticated export/checkpoint binding, authoritative compare-and-
  swap fencing of source N, reservation/consumption by target, and recovery of
  only source-active, source-fenced/target-pending, or target-active. Reject two
  active installations, skipped generations, caller-selected target ids, stale
  source admission, and activation without reachable fencing authority.
- [ ] 1.5 Implement the versioned no-follow local/Hosted cell identity store,
  machine/cell-owned authentication key seam, installation ownership registry,
  owner-only legacy adoption, verified move/rebind, and explicit clone identity
  creation plus fenced failover transfer/generation recovery. Keep all
  identity/private key state outside `Knowledge Base/` and
  portability archives; do not derive trust from path text or request content.
- [ ] 1.6 Add red tests in `tests/test_consolidation_intake.py` for the complete
  `source-export-attestation/v1` claim set and concrete detached Ed25519 mode:
  exact `u32be(len("exomem.source-export-attestation/v1"))||domain||
  u64be(len(JCS claims))||JCS claims` signed bytes, signature excluded from claims,
  raw 64-byte unpadded-base64url signature, and
  private `SourceExportVerifierRecord/v1` binding raw 32-byte unpadded-base64url
  public key, `ed25519-sha256:<lowercase-hex>` key id, purpose, audience,
  source vault/installation/generation, status, not-before/not-after, revocation,
  and registry generation. Cover valid RFC 8032/Exomem fixed vectors, missing or
  unknown fields, wrong schema/algorithm, unknown/premature/expired/revoked/
  wrong-purpose/audience/source key, bounded two-key rotation overlap, invalid
  signature, source vault/installation/Hosted-cell/export mismatch, typed
  cell/vault alias, stale/changed active fence, quiescence mismatch,
  archive/manifest/census mismatch, and
  public-key/revocation custody through retirement. Prove no shared source HMAC
  secret is provisioned. Test the named control-plane-receipt alternative only
  against an equally closed issuer trust record and claim set.
  Pin the verifier purpose to the one exact literal
  `vault-consolidation-export`; reject every other spelling at intake, apply,
  retirement clearance, and retirement consumption.
- [ ] 1.7 Add a red forged-archive differential: keep request input fixed, compare
  a genuine archive with a self-consistent attacker-recomputed archive/manifest,
  and prove only the configured external trust proof admits the genuine source.
  Prove caller-supplied verifier keys, source identity claims, and expected hashes
  do not establish authority and that failures echo no secret, source existence,
  or private path.
- [x] 1.8 Add red property/fuzz coverage around the existing portability parser for
  version/resource bounds, absolute/traversal paths, slash variants, unsafe links,
  duplicate normalized paths, case collisions, unsupported entries, source
  runtime/credential/lifecycle/lease/idempotency/log state, and source-derived
  indexes. Assert the destination and archive are unchanged on every refusal.
- [x] 1.9 Add red tests proving intake accepts only opaque archive/proof references,
  rejects inline archive bytes and active-root or `Knowledge Base/` extraction,
  cannot use a live source MCP crawl as a complete inventory, and returns only
  bounded inventory facts plus content hashes from private extraction.
- [x] 1.10 Extend the portability export seam to calculate the canonical source
  census and issue/accept Ed25519 detached claims through configured verifier
  records/rotation, or the equally exact named control receipt. Hosted binds its
  distinct typed cell id; local omits that Hosted-only field and uses its
  cell/machine-owned key or trusted local control transport. Reuse
  `hosted_portability` manifest verification and extraction rules; exclude private
  identity/key state from the archive and do not add a second archive format or
  caller-selected trust.
- [x] 1.11 Add a reusable consolidation intake adapter that writes only to the
  private artifact abstraction, keeps the archive immutable and content-addressed,
  and never calls restore publication or overlays a destination root. Verify the
  archive and source census are byte-identical before and after every test.
- [x] 1.12 Run the red/green identity/intake set plus existing portability/restore
  regressions:
  `uv run python -m pytest -q tests/test_consolidation_cell_identity.py tests/test_consolidation_intake.py tests/test_hosted_binding_v2.py tests/test_hosted_portability.py tests/test_hosted_restore_candidate.py`.

## 2. Reserved run state, private artifacts, and immutable fingerprints

- [x] 2.1 Add red tests in `tests/test_consolidation_run_state.py` proving each run
  persists owner-only state beneath
  `Knowledge Base/_Consolidation/runs/<run_id>/`, rejects duplicate/conflicting run
  identity, resumes after process restart, pages large inventories deterministically,
  and never stores an absolute artifact path, source body, conflict text, policy
  body, principal id, credential, archive bytes, or rollback-preimage bytes.
- [x] 2.2 Extend `tests/test_reserved_admin_paths.py` with red command-registry and
  filesystem-alias coverage proving every generic read, write, move, copy, delete,
  restore, transfer, adoption, Records, media, dataset, audit, and canonical-ref
  path hides/refuses `_Consolidation` before existence/count/parse effects, while
  only a non-serializable consolidation authority can mutate it.
- [x] 2.3 Add red recall/index tests proving `scope="vault"`, keyword/vector/graph
  rebuilds, review queues, overview counts, resources, exports, and file watchers
  exclude run state and the entire private artifact root. Hold request input fixed
  and prove results are byte-identical with no run versus a run containing matching
  private terms.
- [ ] 2.4 Add fixed-vector and permutation tests for source and destination
  fingerprints. Both content censuses SHALL use the whole structural control/
  evidence exclusions while archive/manifest digests independently bind every
  admitted archive byte. Destination census SHALL include canonical knowledge, append-only
  artifacts, authored history, access/review state, and active policy source; only
  fixed schema exclusions for rebuildable derivatives, runtime state, the entire
  `_Consolidation/**` subtree, every current/older/concurrent receipt chain, all
  seal/journal/control state, and private artifacts may be ignored. Prove receipt/run
  appends do not stale a plan but any included byte/path/type or policy/access/review
  change does. Both source and destination fingerprints SHALL bind logical
  `vault_id`, `installation_id`/generation/active-fence, and authenticated
  identity/root-binding digest;
  a move uses the newly authenticated binding and a clone/copy/installation change
  cannot reuse the old plan. Bind the plan's immutable control-basis/successor-
  automaton digests and each later request's owner-only successor context /
  protected current semantic predecessor separately from physical receipt-head
  churn; prove no rollback restores or
  rewinds any excluded control/evidence subtree.
  Pin the exact framed JCS domains
  `exomem.consolidation-source-fingerprint/v1` and
  `exomem.consolidation-destination-snapshot/v1` and their closed field sets.
- [x] 2.5 Implement the structurally reserved durable run store and local/Hosted
  private artifact abstraction with opaque references, content-addressed manifests,
  ownership/mode/no-follow checks, resource admission, atomic state revisions, and
  explicit missing/mismatched-artifact state. Reuse the Adoption run-store pattern
  and `batch_atomic_write` where compatible without storing source bodies in the
  canonical run.
- [x] 2.6 Implement the canonical census/fingerprint builders as pure functions with
  domain separation, normalized relative paths, deterministic sorting, explicit
  entry types/sizes/SHA-256, and fixed exclusions. Re-read state under guarded
  acquisition rather than trusting caller inventory.
- [x] 2.7 Run
  `uv run python -m pytest -q tests/test_consolidation_run_state.py tests/test_reserved_admin_paths.py tests/test_adoption_run_state_not_knowledge.py tests/test_adoption_run.py tests/test_vault.py`.

## 3. Exhaustive C1-C8 inventory and reconciliation

- [x] 3.1 Add a red golden fixture in `tests/test_consolidation_reconciliation.py`
  containing at least one item in every class C1-C8 and overlap cases proving the
  exact precedence `C8 > C6 > C5 > C4 > C3 > C1 > C7 > C2`. For every row assert
  the primary class, attached dependency findings, exact source/destination
  fingerprints, allowed resolutions, default action where permitted, and stable
  order independent of traversal/dictionary/platform order.
- [x] 3.2 Add red property tests generating path/identity/content/dependency
  combinations. Prove every source object receives exactly one primary class,
  C1-C3 defaults are deterministic and lossless, C4-C8 remain blocked without an
  owner decision, and a row cannot disappear when an index or downstream
  dependency is unavailable or ambiguous.
- [ ] 3.3 Add red fixtures covering stable identity, normalized/case-colliding paths,
  logical content with only `exomem_id` removed, semantic anchors, wikilinks/refs,
  supersession/history, typed relations, citations and reverse citations, Records
  manifests/items/audit chains, media binaries/sidecars/frames, review decisions,
  Sources, Evidence, and authority/control artifacts. Prove a C7 dependency can
  block an otherwise C2 object and C8 always wins for executable authority.
- [ ] 3.4 Add red preservation tests proving Sources/Evidence are never overwritten
  or body-rewritten, exact-byte dedupe is the only destructive dedupe, collision
  relocation preserves byte identity and provenance, compiled-note choices bind
  exact before/after hashes, every renamed identity/path rewrites only planned
  references, and source-derived indexes/caches are rejected rather than copied.
  For every C1 publication no-op, assert durable owner-only mapping from exact
  authenticated source snapshot/object/path/identity/hash to destination
  object/path/identity/hash, inclusion in reconciliation/plan digests, and a
  plaintext-free mapping-set receipt digest through completion and rollback.
- [x] 3.5 Implement pure bounded inventory/index builders and the C1-C8 classifier,
  returning stable typed rows and explicit dependency maps. Reuse canonical
  identity, relation, media, Records, review, and provenance parsers rather than
  adding consolidation-only interpretations.
- [x] 3.6 Implement finite resolution schemas and a deterministic tentative-map
  validator. Reject unresolved conflicts, dangling/ambiguous anchors and edges,
  invalid lifecycle/type transitions, duplicate identities, append-only rewrites,
  and unaccounted inventory rows before a plan can materialize. Keep reasoning and
  suggested resolution prose agent-side.
- [x] 3.7 Run
  `uv run python -m pytest -q tests/test_consolidation_reconciliation.py tests/test_unicode_page_identity.py tests/test_case_insensitive_identity.py tests/test_relation_registry.py tests/test_review_state.py tests/test_records_recall_graph.py tests/test_records_recall_media.py tests/test_media_processing.py`.

## 4. Fresh destination principals and prospective policy

- [x] 4.1 Add red tests in `tests/test_consolidation_policy.py` proving source policy,
  audience ids, grants, credentials, escalation tokens, authorization sessions,
  release approvals, receipts, runtime bindings, and review authority never become
  live destination authority. Include coincident-looking source/destination ids and
  prove provenance retention cannot make them executable.
- [x] 4.2 Add fixed-vector tests for
  `destination-principal-attestation/v1`: trusted destination issuer/surface,
  resolved canonical principal, destination vault, purposes, issued/expiry,
  authentication/session binding, nonce, and attestation fingerprint. Cover
  missing/expired/stale/cross-vault/cross-session/cross-issuer/copied/replayed
  attestations and caller-selected principal/audience fields; every invalid case
  must fail before approval/apply without consuming authority.
- [x] 4.3 Add red prospective-compile tests for the exact newly authored destination
  scope/rule/bridge/exact-release documents, using the hardening change's stable
  conflict-bound acquisition. Cover per-scope grant crossover, default deny, deny
  dominance, overlapping conservative options, non-Markdown unresolved membership,
  pending-policy guard drift, and document/conflict changes between planning and
  apply.
- [x] 4.4 Implement the trusted destination attestation verifier on top of the common
  per-surface authorization context. The surface resolves principal and issuer;
  request data may declare intended purpose but cannot select identity or trust.
- [x] 4.5 Implement policy translation inputs as newly authored destination
  documents compiled through existing governance authoring/bridge/exact-release
  primitives. Store source policy only as authenticated review input/digests and
  reject any copied live-authority artifact.
- [x] 4.6 Run
  `uv run python -m pytest -q tests/test_consolidation_policy.py tests/test_governance_decisions.py tests/test_governance_membership.py tests/test_governance_policy.py tests/test_governance_bridges.py tests/test_governance_tokens.py tests/test_authorization_session_binding.py`.

## 5. Canonical joint plan and single-use owner confirmation

- [ ] 5.1 Add fixed canonical JSON and SHA-256 vectors in
  `tests/test_consolidation_plan.py` for every required
  `exomem.consolidation-plan/v1` field using RFC 8785/JCS over the closed value
  subset: NFC-before-validation, duplicate and post-NFC-duplicate key refusal,
  JCS escaping/UTF-16 property ordering, UTC RFC3339 millisecond strings, bounded
  integer counts/ordinals/generations/byte sizes/TTL at 0..2^53-1, and rejection
  of negative zero, other negative values, float/exponent forms, NaN/infinity,
  and unspecified null. Pin `u32be(domain ASCII length)||domain||u64be(JCS byte
  length)||JCS`, normalized-path input, plan nonce/deadline, and exact digest
  bytes across Python/TypeScript/Go-compatible vectors. Assert `plan_digest` is
  SHA-256 over the framed canonical preimage outside those bytes and no digest
  field recursively hashes itself.
- [ ] 5.2 Add a red mutation matrix that changes each source/destination snapshot,
  preimage census, inventory/reconciliation/decision/identity/path/dependency digest,
  content action/before/after hash, batch partition, policy/bridge/release document,
  principal attestation, disclosure row, positive/negative probe, rollback term,
  required source-retention/recovery-window term, immutable control-basis or
  successor-automaton digest, server impact summary/rendering definition, mode, expiry, or nonce one at a
  time. Every change SHALL produce
  a different plan digest and make the old token unusable.
  Distinguish the cutover-bound nonterminal rollback-contingency digest from any
  later terminal-run rollback plan, which must inventory then-current state.
- [ ] 5.3 Add red approval tests for exact expected-plan binding, expiry, single-use
  JTI reservation, acknowledgement loss, identical operation resume, concurrent
  consume, changed payload, destination drift, and agent/body-supplied
  `approved`, owner, identity, or capability values. Assert only a trusted
  purpose-bound surface confirmation mints a token and persisted/logged state never
  contains token bytes or the confirmation secret.
- [ ] 5.4 Add red trusted-rendering tests proving
  `plan(operation=render)` loads the stored plan by run/kind/digest, derives the
  impact summary, ordered sections, stable page boundaries/digests/totals
  server-side, and records each served/acknowledged page under one authenticated
  owner/session/surface. Missing, skipped, reordered, truncated, duplicated,
  caller-defined, cross-session, or agent-supplied pages/summary cannot mint
  `plan-rendering-completeness/v1`; complete coverage binds every page/section,
  totals, impact-summary digest, plan digest, owner/session/issuer, validity, and
  nonce. Exercise authenticated CLI pager, Hosted confirmation, and MCP/REST host
  elicitation/protected OOB paths; plan page bodies reach only that trusted human
  renderer, never the agent-facing terminal, retry store, receipt, or log.
  Pin surface-injected `PlanRenderAcknowledgement/v1` fields and prove the body
  expected-page digest alone records no coverage even when every digest is known.
  Pin the exact closed JCS automaton object and fixed vector: exact fields
  `schema`, `initial_state`, `states`, `terminal_state`, `minimum_pages`,
  `page_count_source`, `transitions`, `retry_rule`, `unexpected_event_rule`;
  fixed ordered states `plan-materialized -> render-begin -> page(i) -> ack(i)
  -> render-complete -> approval -> token-reservation`; exact seven transition
  objects/guards; minimum page count one; stored rendering-definition source;
  identical-event adoption; and unexpected-event staleness. Its digest uses the
  framed-JCS schema domain and is outside an object containing no run/plan/page/
  basis/self digest. Add a valid red progression with an unrelated receipt advancing
  physical `prev`, plus skipped/reordered/duplicate/replayed page or ack, wrong
  successor context, and unexpected same-run reconcile/new-plan/control event
  cases. The automaton ends at token reservation; apply's separately journaled
  first effect is `seal-intent`. Apply SHALL revalidate the current token-
  reservation predecessor separately while the materialization control basis
  remains an ancestor; it
  SHALL not require the current global receipt head or complete mutable run tree
  to equal the plan-time values.
- [ ] 5.5 Implement canonical plan assembly over immutable run objects, persisting the
  complete preimage owner-only and returning bounded pages plus stable digests/counts.
  Materialize `control_basis_digest` and the closed successor-automaton digest,
  persist every declared event predecessor, and bind the current executor
  predecessor separately at token reservation/apply without hashing mutable
  render/approval rows or the global receipt head into the old plan basis.
  Reuse governance's canonical hashing, exact policy fingerprint, and approval-token
  seams instead of creating an unbounded preview response.
- [ ] 5.5a Add red schema/vector/reachability tests for the closed
  `exomem.consolidation-successor-context/v1` common object, all ten exact
  `context_kind`/`facts` branches, required `context_seed_digest`, framed-JCS
  digest, owner/destination/run/revision/expiry/basis/predecessor/action/variant/
  request-visible fact checks,
  and one-operation reservation/replay rules. Prove only an exact product
  terminal in the shared plan-entry table returns a plan-materialize context and
  advertises `plan`; materialization returns render-begin; each render
  terminal returns its exact next context; completeness returns approve;
  approval returns apply, terminal rollback, or retirement clearance by kind;
  listed complete/repair/rollback/finalize terminal returns the table's exact
  kind set; and eligible sealed nonterminal apply/recover/verify returns the
  separate contingency-rollback context.
  Lost output is recoverable only through owner-detail status, which returns the
  already-durable pair without an event/revision change; summary/non-owner status
  exposes neither key. Pin the exact max-timestamp plan-entry expiry, the
  1..86,400,000 ms child TTL, and plan/token/recovery deadline minima; status
  never extends a context, no action continues an expired non-contingency
  child, and only a later shared-table producer can install a new plan-entry
  pair. Prove
  cross-adapter continuation under a newly
  authenticated same owner succeeds. Missing, inline, mismatched, expired,
  cross-owner, foreign-render-session/surface, out-of-order, consumed, or
  unexpectedly intervened contexts fail before effect. Assert
  protected predecessor/publication/journal/contingency
  authority facts never appear in request or plaintext terminal/status output.
  Pin destination binding to the snapshot identity/root fingerprint; pin the
  exact closed stable framed-JCS successor-owner-binding fields/domain (excluding
  session/surface so cross-adapter retries remain possible); and assert
  `basis_digest` is respectively the plan-input basis, relevant plan control
  basis, or original apply journal digest for the normative branch.
  Add closed-schema and cross-runtime fixed vectors for
  `exomem.consolidation-successor-context-seed/v1`: exactly its fourteen fields,
  exact full-context schema link, branch-identical `facts`, predecessor/full-
  context/ref/receipt exclusions, and the seed-domain digest. Pin derivation of
  the full context by adding seed digest plus exact committed terminal id/payload
  digest. Prove the terminal payload vector changes with `S` but contains no
  full-context commitment, while the full-context vector changes with that
  already-final terminal: the only accepted graph is `S -> P -> context`.
- [ ] 5.5b Generate table-driven red plan-entry reachability and receipt-causality
  tests from this one closed table. For every row/current-terminal/product-
  terminal combination, commit the terminal/context journal final, assert the
  exact pair is returned, resolve its predecessor into the matching plan intent,
  and accept materialization. For every nonrow product action, phase, rollback
  mode, repair target, run mode, eligibility state, and semantic terminal, assert
  no pair, no `plan` next action, stale/refused materialization, and no plan
  intent. Exercise the status-only pending-forward checkpoint and prove its next
  retirement event stales the pair. At every producer inject gaps after the
  inert seed/ref reservation, intent, prepared journal, effect, classification,
  terminal, full-context run-journal final, and, where that producer completes a
  product action, its action-level idempotency terminal. Assert the seed-only state is unresolvable; terminal-before-final
  reconstructs one byte-identical context without an event/revision; final-
  before-idempotency writes only byte-identical `T`; status returns no pair
  before final and exactly the durable pair afterward. Tampered or cross-bound
  seed/ref/terminal/context/idempotency state fails closed.

| Plan kind | Exact allowed current committed semantic terminal kind(s) | Exact product logical terminal(s) that durably return the `plan-materialize` context pair |
|---|---|---|
| `cutover` | `reconcile` with `unresolved=0` and cutover eligibility; `repair-terminal` that commits/adopts such a `reconcile` | `reconcile(phase=reconcile)`; `recover(phase=repair-terminal)` only for that repair; `status(detail=owner-detail)` observing either allowed current terminal |
| `rollback` | `complete`; `rollback-complete`; `retirement-pending-forward-only`; `retirement-finalize`; `repair-terminal` that commits/adopts one of those four kinds | at `complete`: `apply(phase=complete)` or `verify(verification_kind=transport,phase=complete)`; at `rollback-complete`: `rollback(rollback_mode=nonterminal-contingency,phase=rollback-complete)` or `rollback(rollback_mode=terminal-plan,phase=rollback-complete)`; at `retirement-pending-forward-only`: `status(detail=owner-detail)` only; at `retirement-finalize`: `retire-source(phase=finalize)` whose terminal phase is `retirement-finalize`; at an allowed repair: `recover(phase=repair-terminal)`; at every allowed current terminal: `status(detail=owner-detail)` |
| `retirement` | real-cutover `complete`; `repair-terminal` that commits/adopts real-cutover `complete` | `apply(phase=complete)` or `verify(verification_kind=transport,phase=complete)` only when the current run is real-cutover and retirement prerequisites are eligible; `recover(phase=repair-terminal)` only for that repair and eligibility; `status(detail=owner-detail)` observing either allowed current terminal and eligibility |

This table is closed. Every effect whose committed target creates a successor
context SHALL first create or adopt, inside the already-required owner-only
idempotency reservation, one inert future-context reservation containing an
opaque future ref, canonical seed bytes, and seed digest `S`. The closed seed
JCS object has schema `exomem.consolidation-successor-context-seed/v1` and
exactly `schema`, `context_schema`, `context_kind`, `run_id`, `run_revision`,
`destination_binding_digest`, `owner_binding_digest`, `basis_digest`,
`successor_action`, `successor_variant`, `issued_at`, `expires_at`, `nonce`, and
`facts`. `schema` equals the seed schema, `context_schema` equals
`exomem.consolidation-successor-context/v1`, and every other value equals the
eventual full context value. The seed MUST NOT contain
`context_seed_digest`, `predecessor_event_id`, `predecessor_payload_digest`, a
full-context digest/ref, or any receipt id/hash/head. `S` is SHA-256 over the
common length-prefixed frame with exact ASCII domain
`exomem.consolidation-successor-context-seed/v1` and the seed JCS bytes.

The inert reservation is fsynced before the intent, changes no semantic state
or run revision, and cannot be resolved or returned. It is keyed by the
existing vault/installation-generation/owner/operation reservation and also
binds action, canonical request digest, run, effect kind, and effect ordinal;
only that byte-identical operation may adopt it. The successor-producing
intent's target-digest preimage and nested payload contain
`successor_context_seed_digest=S`; its prepared journal and matching terminal
carry the same `S`. That field is required for an intent whose target would
create a context and its committed or aborted terminal, and forbidden for every
other effect. Neither receipt role, `target_digest`, nor `observed_digest`
contains the full context digest/ref or the not-yet-existing terminal
id/payload digest.

After the committed terminal is fsynced, let `P` be its already-final nested
payload digest. The coordinator deterministically derives the full context by
copying every non-schema seed field, setting `schema=context_schema`, adding
`context_seed_digest=S`, `predecessor_event_id` equal to that committed outer
terminal id, and `predecessor_payload_digest=P`, then computes
`successor_context_digest` under the full-context domain. The run-journal final
SHALL fsync the seed bytes/digest, opaque ref, full context bytes/digest, and
terminal outer id/payload digest/hash/sequence/new physical head together. That
final is the sole point at which the context becomes current or resolvable; the
derivation emits no second semantic event and does not advance run revision.
Only after that final may the idempotency record fsync canonical logical
terminal `T`, whose trusted output binds the opaque ref and full-context digest,
and only then may a product terminal return it. Receipt intent/terminal bind
`S`; journal final binds `S`, `P`, and the full-context digest; product output
binds the ref/full-context digest and does not expose `S`. The dependency graph
is therefore `S -> P -> full context`, never `full context -> P`.

Recovery treats a seed reservation without an intent as inert; an identical
retry adopts it and changed identity/request/effect data conflicts. The existing
intent/prepared/effect/classification recovery rules then apply. A committed
terminal without final deterministically reconstructs only the same full
context from its reserved seed plus terminal id/`P` and writes final without a
new event, effect, or revision. A final without the idempotency logical terminal
writes only byte-identical `T`; retry then reports replayed delivery. An aborted
terminal or nonrow effect never materializes a context. Missing, divergent, or
cross-bound seed bytes/digest, ref, terminal, full-context digest, journal, or
idempotency data fails closed before return or successor admission.

For an allowed terminal, one context may list both `rollback` and `retirement`
at eligible real-cutover `complete`. At the internal
`retirement-pending-forward-only` checkpoint, the pair is returnable only after
the same durable derivation and only by owner-detail status; continuing
retirement emits a successor and stales it. A product terminal not listed for
the exact row, phase, mode, target repair, run mode, or eligibility SHALL return
no plan-materialize pair and cannot be a plan intent parent. In this table,
`repair-terminal` that commits/adopts kind K means its preceding
`recover-classification` references K's unresolved intent/terminal, exact
classification observes K's target digest, and the committed repair payload
binds that reference and observed digest; no caller field selects K.
- [ ] 5.6 Implement trusted stored-plan rendering/completeness plus purpose-bound
  owner confirmation and token reservation with the
  existing durable idempotency/security authority. CLI uses authenticated TTY or
  protected out-of-band input; REST/MCP/Hosted adapters inject trusted context;
  caller request fields never become confirmation. A retry may resume only the one
  recorded operation for the JTI.
- [ ] 5.7 Run
  `uv run python -m pytest -q tests/test_consolidation_plan.py tests/test_governance_tokens.py tests/test_governance_store.py tests/test_writer_lease.py tests/test_command_surface_retry.py`.

## 6. Owner-inclusive destination seal and closed-world release coverage

- [ ] 6.1 Add a red closed-world matrix in `tests/test_consolidation_seal.py` generated
  from every command and finite selector plus REST/MCP resources/templates,
  transfer/upload/download, raw bytes, media/frame, Records/dataset, review,
  graph/context, export, error adapter, and background writer. Assert each branch
  declares sealed disposition; a synthetic new content branch without one fails
  startup/coverage instead of defaulting open.
- [ ] 6.2 For every ordinary read branch, add owner and delegated cases proving
  sealing drains admitted reads then returns the same content-free outcome to all
  new callers. Explicitly cover owner ask/read/raw get/browse/media/review/graph/
  history/Records/export/file operations, empty-policy fast path, L6/full release,
  exact-release approval, escalation token, not-found, validation, collision,
  timeout, cancellation, and internal error.
- [ ] 6.3 Add differential tests in `tests/test_consolidation_oracle_closure.py` that
  hold principal, purpose, request bytes, and seal state fixed while varying absent
  versus present/private items and policy, batch, recovery, mixed-journal, and
  conflict states. Compare complete response bytes/length, counts, status/code,
  remediation, and bounded timing buckets; no run id, phase, path, title, ref,
  snippet, score, identity, policy fact, item existence, or recovery branch may
  differ.
- [ ] 6.4 Add restart/concurrency tests proving seal intent is durable before drain,
  new reads/writes/transfers/background work stop admitting, already-admitted work
  drains within a bound, failure to drain publishes nothing, and the persisted seal
  loads before readiness after a crash in every nonterminal phase. Model the
  effective typed union `open | deletion-sealed(checkpoint) |
  consolidation-sealed(vault,run,operation,phase,journal)`: deletion dominates,
  only the exact consolidation journal can unseal its own kind, and generic
  resume/consolidation recovery never reopens deletion. Other canonical vault
  identities remain independently available.
- [ ] 6.5 Implement one durable seal/admission state integrated with the shared
  lifecycle and process-safe writer boundary. Place the outer sealed response before
  retrieval, raw serialization, enumeration, mutation dispatch, and error assembly;
  retain existing release decisions/projectors/scrubber underneath it and keep
  deletion-seal semantics irreversible.
- [ ] 6.6 Add red Hosted owner-control admission tests in
  `tests/test_consolidation_control_admission.py` proving apply and its
  identical tagged-request/operation-id resume,
  verify, recover, abort, and rollback never use the ordinary full-leaf
  `admit_mutation()` wrapper. Apply SHALL authenticate owner, reserve operation/
  JTI/writer-lifecycle authority, then atomically convert exactly itself out of
  the ordinary active-mutation counter before draining every other participant.
  Cover conversion races/crashes, other active writers, lost acknowledgement,
  and an E2E timeout proving no self-deadlock at `active_mutations == 0`.
  After sealed restart, prove the private owner-control lane reaches only status,
  recover, abort, rollback, verify, or the identical necessary apply resume after
  owner-before-run lookup; new apply, non-owner, generic dispatch, and deletion-
  seal crossing remain refused. Exercise rollback's closed modes through this
  lane: both modes require an opaque successor-context pair;
  `nonterminal-contingency` resolves it server-side to the exact still-sealed
  original apply journal, cutover/contingency/publication-state/revision/current
  predecessor and reserved authority while forbidding those protected body
  fields plus rollback plan/token; `terminal-plan` requires its separately
  rendered plan/token and resolves the approval predecessor from its context
  while forbidding every contingency field. Kill/restart before admission and after
  reservation, then prove each mode remains reachable, single-use/idempotent, and
  disjoint without widening generic dispatch.
- [ ] 6.7 Implement the journaled control-mutation admission/conversion and sealed-
  restart owner-control lane. Exactly one operation may be excluded from the
  ordinary active counter; phase-bound batches retain exclusive authority and a
  crash cannot leave the operation both counted/excluded or neither owned nor
  recoverable. Treat any lifecycle action registered through the ordinary
  full-leaf wrapper as a coverage/startup failure.
- [ ] 6.8 Implement an unforgeable, non-serializable, vault/run/journal/phase/action-
  bound `ConsolidationAuthority`. Add red construction/serialization/request-field/
  wrong-phase tests and prove a named pre-unseal probe remains in-process, bypasses
  only the outer seal, and calls the same adapter/serializer functions while normal
  identity resolution, policy, projection, scrubbing, response, and receipts still
  execute. Explicitly prove the object cannot serialize into or be reconstructed
  from MCP, REST, CLI, Hosted, retry, journal, or receipt data.
- [ ] 6.9 Run
  `uv run python -m pytest -q tests/test_consolidation_seal.py tests/test_consolidation_control_admission.py tests/test_consolidation_oracle_closure.py tests/test_governance_egress.py tests/test_governance_postfilter.py tests/test_get_payload.py tests/test_hosted_lifecycle_regressions.py tests/test_hosted_transfer_v2.py`.

## 7. Policy-first journaled saga and complete preimage

- [ ] 7.1 Add a red state-machine/property suite in
  `tests/test_consolidation_saga.py` enumerating every legal and illegal transition
  in `approved -> sealing -> sealed -> preimage-ready -> policy-active ->
  publishing -> rebuilding -> verifying -> verified -> transport-stopping ->
  transport-verifying -> transport-verified -> routing-opening -> complete`.
  Assert no transition skips a durable predecessor, no ordinary content is
  observable in any intermediate state, exact-cell transport runs only while
  public routing is durably stopped/drained, and illegal/repeated/changed
  transitions fail without a second effect.
- [ ] 7.2 Add red preimage tests covering policy, canonical knowledge, Sources,
  Evidence, Records, media/sidecars, identity/history/relations/citations,
  access/review state, canonical metadata, empty files, non-Markdown bytes, and
  platform-normalized paths. Cover insufficient space/quota, partial artifact,
  missing entry, changed byte, unsafe link, lost artifact, and manifest mismatch;
  policy/content publication must remain untouched until the full preimage
  verifies. Exclude the entire `_Consolidation/**` subtree, every receipt chain,
  and all seal/journal/control state from census/preimage/rollback while binding
  the immutable plan control basis and each effect's exact current semantic
  predecessor/outer receipt references separately; prove no restore touches an
  older or concurrent run/evidence subtree and no unrelated physical-head append
  changes the canonical preimage.
- [ ] 7.3 Add a red ordering test that instruments the existing governance
  transaction and `batch_atomic_write`: restrictive policy intent/prepare/activate/
  critical terminal must finish before the first content batch call. Assert the
  approved deterministic partition and each batch's prior/prepared/final/action
  fingerprints, journal transition, and publication-boundary ordinal.
- [ ] 7.4 Add red multi-batch failure tests at every before/after prepare, rename,
  metadata, journal, receipt, and acknowledgement seam. State may be prior,
  prepared, or final only; a mixed/third state stays sealed and never produces a
  false whole-saga atomicity claim.
- [ ] 7.5 Add red rebuild tests proving lexical, embedding, semantic-unit, graph,
  media, freshness, identity, and review derivatives are recreated only from
  canonical destination bytes after all content batches, source-derived databases
  are never installed, and canonical bytes cannot be changed by rebuild.
- [ ] 7.6 Implement the saga coordinator over the existing governance transaction,
  receipt-first critical-event, writer boundary, and bounded
  `batch_atomic_write` primitives. Persist phase/action fingerprints before effects,
  reserve the approval JTI/operation once, keep the typed consolidation seal
  throughout ordinary publication, use only the phase-bound routing-stopped
  suspension for exact-cell transport verification, and treat the first committed
  content batch as the irrevocable abort/rollback boundary.
- [ ] 7.7 Run
  `uv run python -m pytest -q tests/test_consolidation_saga.py tests/test_consolidation_preimage.py tests/test_vault.py tests/test_govern_memory_tool.py tests/test_governance_recovery.py tests/test_graph_idempotency_crash_matrix.py tests/test_media_jobs.py`.

## 8. Exact recovery, abort, rollback, and source checkpoint disposition

- [ ] 8.1 Add `tests/test_consolidation_recovery.py` with a deterministic crash matrix
  before/after JTI reservation, seal intent/drain, preimage proof, policy
  intent/prepare/activate/terminal, every content batch phase, every derived rebuild
  class, every in-process verification class, transport stop/probe/terminal/routing
  open, abort restore, every rollback plan/render/approval/restore/rebuild/probe,
  and retirement plan/forward-snapshot/surviving-copy-ledger/pending-fence/
  clearance/consume/completion/fence-release/finalize. Restart SHALL load
  routing-stop and the typed seal before ordinary readiness, classify exact prior/
  prepared/final state without repeating mutation or semantic reasoning, and keep
  deletion-sealed cells irreversible.
- [ ] 8.2 Add red changed-retry cases for operation id, request digest, run/mode,
  source/destination identity/fingerprint, archive/proof/artifact ref, attestation,
  decision, plan/token/JTI, batch partition, probe, rollback term, and confirmation.
  Only byte-identical input under the same canonical owner and an exact existing
  operation reservation may resume under a newly verified owner context; its
  session/surface may differ for cross-surface lost-ack recovery. A changed owner,
  an unreserved pre-confirmation session mismatch, or every other changed retry
  conflicts while the destination remains safe/sealed.
- [ ] 8.3 Add red abort tests before zero and after policy activation but before the
  first content batch. Assert candidate staging cleanup, exact prior policy/census
  restoration, derivative readiness, aborted token/journal terminal, source
  immutability, and unseal only after proof. Assert abort is refused at and after the
  first committed content batch.
- [ ] 8.4 Add red rollback tests after each publication phase and after complete
  verification. Assert complete preimage restoration, derivative rebuild, exact
  prior policy/access/review/census, source preservation, and append-only governance,
  consolidation, mutation, disclosure, and deletion receipts; preimage restore must
  never replace/truncate an evidence chain or sidecar head.
- [ ] 8.5 Add canonical/fixed-vector tests in
  `tests/test_consolidation_rollback_plan.py` for
  `exomem.consolidation-rollback-plan/v1` and its separately computed SHA-256
  framed-JCS digest under that exact ASCII domain. Cover schema/protocol, run and plan-materialization operation,
  target/deadline/nonce,
  original cutover plan/terminal, source/destination identity generations and
  snapshots, current/pre/post/target census-policy-access-review-manifest state,
  source/archive retention and retirement mode, forward-snapshot proof, and the
  sorted union of every current, target, imported/C1-mapped, and post-cutover
  created/modified/moved/deleted object. Require each row's exact origin/current/
  target hashes, dependency/conflict, before/after, surviving-copy proof, and one
  treatment: restore target, retain current at an exact collision-free path,
  reapply current, retain both, or owner-confirmed discard. Bind deterministic
  batches, target policy, rebuild/probes/receipts, rollback-of-rollback artifact,
  server impact summary, and trusted rendering definition. Missing/default rows
  or options SHALL fail materialization.
- [ ] 8.6 Add red rollback plan/approval/execution transitions in
  `tests/test_consolidation_rollback_plan.py` for
  `plan(plan_kind=rollback,operation=materialize|render)`, complete trusted
  section/page acknowledgement, `approve(plan_kind=rollback)`, single-use JTI,
  and `rollback-approved -> rollback-sealing -> rollback-revalidating ->
  rollback-restoring -> rollback-rebuilding -> rollback-verifying ->
  rollback-complete`. Prove identical operation/request is idempotent while any
  changed target, census, treatment, token, retention/no-loss proof, identity, or
  owner conflicts; there is no hidden repair endpoint. Exercise the command's
  closed rollback `oneOf`: both modes require a successor-context ref/digest;
  `nonterminal-contingency` resolves its protected original apply operation/
  journal, cutover-plan/contingency, publication state/run revision/current
  predecessor, deadline, and owner-only reserved authority server-side, forbids
  those body fields plus rollback-plan/token, and consumes no new rollback token.
  `terminal-plan` requires the separately rendered/approved rollback plan/token,
  resolves its approval predecessor from context, and forbids original-apply/
  contingency fields. Test
  exact terminals, lost acknowledgements, restart reachability, mode change under
  one operation id, mixed fields, stale predecessor/authority, and single use for
  both branches.
- [ ] 8.7 Add the required truthful post-unseal differential: complete cutover,
  unseal, make one unrelated canonical destination write, then request rollback.
  Assert `ROLLBACK_RECONCILIATION_REQUIRED`, zero restore writes, byte-identical later
  work, and a new full union inventory/exact trusted-rendered owner-reviewed rollback plan
  that explicitly treats every later write and every imported object. Also prove
  only the cutover's already approved nonterminal contingency may recover under
  its exact sealed state through `rollback_mode=nonterminal-contingency`; every
  terminal-run rollback uses `rollback_mode=terminal-plan` and still requires the
  explicit rollback-plan action even when the census equals recorded post-cutover
  state.
- [ ] 8.8 Add no-loss retirement/rollback tests in
  `tests/test_consolidation_retirement.py`. In
  `pre-cutover-reversible`, retain and revalidate authenticated source/archive
  plus verifier history through the rollback window and permit pre-cutover target
  only while it survives the effect. Before source/archive can become
  irrecoverable, require `forward-only` with a separately retained, content-
  addressed, census-verified `post-cutover-forward-snapshot/v1` containing every
  destination/imported byte and C1/identity/relation/history/citation/review/
  provenance/policy/access/trust bundle; finalization permanently rejects
  pre-cutover rollback. Before issuing forward-only clearance, persist
  `retirement-pending-forward-only/v1` with lifecycle/snapshot/ledger/census/
  source proof/deadline and reject pre-cutover planning immediately. Crash/lost-
  completion recovery keeps it; expiry releases it only with authenticated proof
  the JTI was never consumed and unchanged source/archive survives. At clearance, consume, completion, rollback planning, and
  rollback commit, compute a per-imported-bundle surviving-copy ledger and refuse
  before the first effect if any result would be zero. Prove a pre-cutover
  destination preimage never counts as an imported copy.
  Add fixed vectors for closed schema/domain
  `exomem.consolidation-surviving-copy-ledger/v1`, binding run/plan/effect,
  source/destination/archive/forward proof digests, and every sorted bundle's
  survivor kind/proof/disposition/count; reject missing/duplicate/unverified/zero
  rows and keep only ledger digest/bounded counts in receipts.
- [ ] 8.9 Add source-checkpoint tests for explicit release, continued/reacquired
  quiescence, and retirement handoff. A released-then-changed source census SHALL
  stale cutover/retirement; identical disposition retry is idempotent; changed
  source/checkpoint/disposition conflicts. Bind an exact source-archive choice:
  retained under an opaque verified ref/term, transferred to a named trusted
  custodian with `archive-custody-receipt/v1`, or externally destroyed only after
  clearance. Pin the custody receipt's closed custodian/retention-domain,
  source-vault/installation/generation, archive/manifest/source-census, transfer-
  operation, retention-terms, accepted/issued/not-before/expiry, nonce, and signer
  claims with `not_before <= accepted_at <= issued_at < expires_at`; exact
  framed-JCS Ed25519 bytes/raw unpadded-base64url signature; and
  private `ArchiveCustodianVerifierRecord/v1` raw key/derived key id, purpose,
  audience/source scope, status, validity, revocation, registry generation, and
  bounded two-key overlap. Add valid/expired/revoked/wrong-domain/wrong-artifact/
  missing-history fixed vectors. The review
  must state that destination bytes, owner-only source-to-destination/C1 mappings
  and identity/snapshot/attestation digests, and plaintext-free receipts remain,
  while source-only bytes/control state and archive-only provenance become
  irrecoverable after destruction. No Exomem path deletes source storage, archive,
  backups, keys, account, or billing state.
- [ ] 8.10 Add canonical retirement-preimage fixed vectors and a one-field mutation
  matrix in `tests/test_consolidation_retirement.py`
  for schema/version, nonce/deadline, run, real-cutover plan/terminal,
  source/destination vault/installation/snapshot/current-census, rehearsal/rollback
  proof, destination verification/preimage artifact/readiness, unchanged source
  checkpoint, disposition/terms, post-retirement rollback mode, forward-snapshot
  proof, surviving-copy-ledger digest, and retained-versus-irrecoverable statement
  digest. Pin schema/domain `exomem.consolidation-retirement-plan/v1`, compute
  its SHA-256 framed-JCS digest outside the canonical bytes, and prove only
  a fresh third human confirmation over a completely trusted-rendered stored plan
  clears retirement.
- [ ] 8.11 Add red single-use retirement-consume tests in
  `tests/test_consolidation_retirement.py`. Clearance is a purpose-bound
  source-lifecycle capability containing retirement/destination/no-loss/
  verification proof digests, source checkpoint/census, archive disposition/
  artifact digest, rollback mode/forward-snapshot proof, operation id, JTI,
  deadline, and source audience. Before any external destructive step, consume it
  under source lifetime/fencing authority and revalidate every field plus current
  destination recovery and, for forward-only, the pending destination rollback-
  fence digest. Drift, expiry, replay, changed JTI/operation, or stale
  fence performs nothing. Issuance, consume, external
  `source-retirement-completion/v1`, and destination finalize SHALL be distinct
  schemas/terminals and independently idempotent; Exomem deletes no external
  routing/storage/archive/key/backup/account/billing resource. Pin completion to
  lifecycle ref, clearance JTI/digest, source vault/installation/generation/
  consumed fence, disposition/artifact digest, completion operation/outcome/time,
  source consume event id/digest and receipt-head proof, issuer/audience, and
  authentication-proof digest. Prove the source consume chain names destination
  clearance, destination completion names authenticated source consume, local
  `prev` chains remain independent, and no cross-machine atomicity is assumed;
  wrong/untrusted/caller-made
  completion cannot finalize.
  Independently revalidate any counted custody receipt, current verifier record,
  artifact/manifest/census, retention terms, and validity at retirement-plan
  materialization, clearance, consumption, rollback-plan materialization, and
  rollback commit; inject expiry, revocation, terms/artifact drift, and key
  rotation between each pair of gates and prove cached earlier success cannot
  preserve a survivor or authorize an effect.
- [ ] 8.12 Implement the explicit rollback planner/render/approval/executor,
  treatment validator, forward-snapshot store, surviving-copy ledger, and the
  pending-to-permanent forward-only rollback fence. Reuse the existing action variants and common plan/
  confirmation/journal primitives; implement both explicit tagged rollback modes
  and the custody-receipt verifier/revalidation gates without adding a hidden
  rollback route.
- [ ] 8.13 Implement exact recovery classifiers, phase-bound forward/abort/rollback
  actions, census/transport/routing-gated public reopening, retirement capability
  issuance/consumption/finalization, and portability checkpoint disposition.
  Recovery reads only pinned decisions/bytes/fingerprints and never invokes an
  agent or live semantic planner.
- [ ] 8.14 Run
  `uv run python -m pytest -q tests/test_consolidation_recovery.py tests/test_consolidation_rollback_plan.py tests/test_consolidation_retirement.py tests/test_consolidation_saga.py tests/test_governance_recovery.py tests/test_governance_receipts.py tests/test_hosted_lifecycle_regressions.py tests/test_hosted_portability.py`.

## 9. Plaintext-free monotonic consolidation evidence

- [ ] 9.1 Add closed schema/fixed-vector tests in
  `tests/test_consolidation_receipts.py` for authenticated intake, snapshot binding,
  reconciliation, each cutover/rollback/retirement plan, every trusted render page/
  acknowledgement/completeness marker, approval/JTI, seal/drain/preimage, policy,
  every batch/rebuild/in-process probe, transport stop/each exact-cell probe/
  terminal/routing open, abort, every rollback restore/rebuild/probe, recovery,
  retirement forward-snapshot verification, each surviving-copy-ledger,
  pending-forward-only fence installation, clearance/consume/completion/finalize,
  expiry/non-consumption fence release, and the finalize target containing
  permanent fence conversion. Preserve the existing outer `receipt/v1` envelope
  and require its sole consolidation payload member to be one closed
  `exomem.consolidation-event/<kind>/v1` schema per effect, including disposition,
  forward-snapshot/surviving-copy-ledger, rendering/impact, and verification-basis
  digests where applicable. Pin outer schema/event-type/phase/timestamp/instance/
  seq/physical-prev/durability/hash fields separately from the nested schema,
  require `successor_context_seed_digest` exactly on an intent whose target
  would create a context and its matching terminal, forbid it on every other
  payload, and prove receipt/target/observed preimages contain no full-context
  digest/ref or terminal self-reference. Pin the predecessor-free seed and full-
  context fixed vectors separately,
  and reject a nested schema masquerading as the envelope. Reject unknown fields/types and scan JSONL, anchors, journals, sidecars,
  logs, errors, metrics, traces, idempotency rows, and repr/debug output for exact
  fixture bodies, paths, refs, titles, conflict text, raw principals, policy text,
  credentials, token bytes, staging paths, and preimage bytes.
- [ ] 9.2 Add red receipt-first ordering and crash tests for every effect named above.
  Pin nested `payload_digest` to framed JCS domain
  `exomem.consolidation-event-payload/<kind>/<record_role>/v1`. Pin each outer
  intent id to 64 lowercase hex under
  `exomem.consolidation-event-id/<kind>/v1` over the exact closed intent identity:
  schema/kind/run/operation/phase/intent role, bounded effect and applicable
  batch/rebuild/probe/page ordinal, request/prior/optional-prepared/target digests,
  plus non-caller-selected semantic-parent outer id/payload digest. Preserve the
  outer terminal ids exactly as `<intent-id>:committed|aborted`; do not generate a
  second 64-hex terminal id. Make nested role branches closed: intent forbids
  observed digest; committed/aborted requires it and uses a distinct role payload
  digest. Add fixed intent, committed, and aborted vectors across runtimes.
  Assert outer `prev` is always the actual current local record hash, while nested
  semantic parent follows the closed effect table independently: only start uses
  the fixed framed-empty-JCS semantic root, every later intent names its declared
  predecessor terminal, and each terminal names its intent. Interleave unrelated
  receipts to make physical and semantic parents differ validly; reject caller-
  supplied, missing, skipped, reordered, replayed, or duplicate semantic
  successors. For each successor producer first crash after the inert seed/ref
  reservation and prove it is unresolvable and identical-operation-only. For
  each effect then crash after: (1) outer intent fsync plus observed hash/
  sequence/head and conditional seed, (2) prepared-journal fsync referencing
  outer id/hash/seq, nested digest, semantic parent, conditional seed, and state
  digests, (3) the one effect, (4) exact classification, (5) exactly one suffix-
  named committed/aborted outer terminal fsync with nested observed state and
  conditional seed, (6) final-journal fsync referencing terminal outer id/hash/
  seq/nested digest/new physical head and, only for a committed successor
  producer, the derived full context/ref/digest, and (7) action-level
  idempotency-terminal fsync when this effect returns the product logical
  terminal. Prove correct recovery
  for intent-without-prepared, prepared+prior, prepared+target, terminal-without-
  final, final-without-idempotency-terminal, JSONL-ahead anchor adoption,
  missing/divergent seed/context/heads, and mixed/third state. Terminal-without-
  final SHALL derive the same context from seed plus terminal without another
  receipt/effect/revision; final-without-idempotency SHALL store only identical
  `T`. No JSONL/anchor/SQLite-idempotency/run-state/artifact/policy-journal/
  filesystem atomicity may be
  assumed and no semantic effect may replay during repair. Drive every row of
  the shared plan-entry table from task 5.5b: the plan intent parent must equal
  the returned context predecessor for each direct and repaired terminal,
  including rollback-complete, retirement-pending-forward-only, and retirement-
  finalize. Exhaustively cross each plan kind with every nonrow evidence kind and
  repair target and assert no context/plan intent is written.
- [ ] 9.3 Add red tamper/truncation/conflict/mixed-state tests proving critical append,
  recovery, and unseal fail closed while the last verified active policy still
  enforces. Add policy-cache tests proving `_Consolidation`, seal/journal state,
  private artifacts, the entire current/older/concurrent receipt families, and
  receipt churn are not knowledge or prospective/active policy input and do not
  stale the destination plan fingerprint. Bind immutable control-basis/successor-
  automaton digests and each action's current semantic predecessor/outer append
  facts separately, without comparing a later global head or full mutable run tree
  to plan materialization; prove no preimage/abort/rollback restores any excluded
  subtree.
- [ ] 9.4 Add red abort/rollback/downgrade tests proving evidence schema versions and
  chains are monotonic, preimage restoration cannot rewind the receipt head, unknown
  newer records survive a compatible older recovery reader, and a rollback appends
  causally linked evidence for the attempted cutover.
- [ ] 9.5 Implement versioned consolidation payload validators and append/reconcile
  adapters on the existing receipt chain/critical-event protocol. Add only the
  `event_type=consolidation` payload registry entry beneath the unchanged
  `receipt/v1` envelope, preserve 64-hex intent and suffix terminal ids, and keep
  physical append chaining separate from nested semantic causation. Store detailed
  item/conflict/probe facts only in owner-protected run state; receipts contain
  digests/counts/outcome classes only.
- [ ] 9.6 Run
  `uv run python -m pytest -q tests/test_consolidation_receipts.py tests/test_governance_receipts.py tests/test_governance_store.py tests/test_governance_policy.py tests/test_adoption_run_state_not_knowledge.py`.

## 10. Positive and negative cutover verification

- [ ] 10.1 Add `tests/test_consolidation_verification.py` with a representative
  principal-by-purpose-by-item matrix that proves positive owner access, every
  allowed delegated domain, and only explicitly approved compiled abstractions.
  Include fresh destination principal/session attestations and exact expected
  disclosure levels/projections for each positive row.
- [ ] 10.2 Add negative rows for private bodies, source-only provenance, denied
  domains, wrong/cross-purpose requests, stale/cross-session authorization,
  unresolved non-Markdown membership, paths/refs/titles, relations/history,
  Records/dataset rows/counts, media/frames, graph rank/reachability, raw reads,
  resources, exports, errors, pagination/counts, and bounded timing. Use same-input
  present-private versus absent pairs and compare complete wire representations.
- [ ] 10.3 Prove named pre-unseal probes cross only the outer seal in-process:
  instrument the same canonical surface adapter/serializer identity resolution,
  authorization-session verification, release decision, projector, terminal
  scrubber, response adapter, and receipt collector and assert each runs. Force
  each component to deny/fail independently and prove verification fails and the
  destination remains sealed. Add explicit negative tests that no internal
  authority is present in an external request, response, retry record, or wire log.
- [ ] 10.4 Add coverage generation from the command/selector and non-command route
  registries. A newly added content branch without a positive/negative probe
  disposition, seal disposition, projector/reduction adapter, receipt outcome, and
  tombstone gate SHALL fail the gate before shipping.
- [ ] 10.5 Implement bounded verification-plan execution with phase-bound internal
  authority and content-free durable outcomes. Unseal eligibility requires every
  mandatory integrity and disclosure row; skipped/unavailable optional-model lanes
  may not skip keyword/graph/raw/security rows. Keep disposable/clone black-box
  MCP/REST/CLI/Hosted parity as supplemental release evidence using normal
  authentication, never as a transport of the internal capability.
- [ ] 10.6 Add red exact-cell transport-boundary tests in
  `tests/test_consolidation_transport_verification.py` for durable
  `transport-stopping -> transport-verifying -> transport-verified ->
  routing-opening`. Bind exact destination post-cutover census, release/build,
  selected profile/descriptor, configuration/trust/principal mappings, and
  trusted routing-stopped/drained proof. For Hosted, additionally bind and
  revalidate the signed profile-selection record/verifier generation plus its
  exact owner-entitlement-verifier and transport-supervisor readiness digests
  before transport-stop and routing-open; inject signer revocation and each
  readiness drift. Under control-plane supervision,
  temporarily suspend only the exact consolidation seal while public ingress
  remains stopped and run normal-auth black-box MCP, REST, Hosted, and CLI
  positive/negative calls on that exact cell with no serialized authority or
  privileged principal shortcut through a supervisor-owned isolated listener/
  route absent from request auth. Prove arbitrary public/local clients and
  non-precommitted commands remain blocked during the window. Persist basis-bound outcomes. Probe/basis/
  receipt failure or a crash at every seal-suspend/probe/terminal/routing edge
  SHALL never open traffic, SHALL re-seal or retain owner recovery/rollback, and
  SHALL prove clone evidence is not substitutable.
- [ ] 10.7 Implement the routing-stop/drain proof, phase-bound exact-operation seal
  suspension, normal-auth exact-cell probe coordinator, bound transport terminal,
  and routing-open admission. Startup SHALL consume the durable transport state
  before advertising readiness and generic resume SHALL not reopen a deletion-
  sealed or incomplete transport-verifying cell.
- [ ] 10.8 Run
  `uv run python -m pytest -q tests/test_consolidation_verification.py tests/test_consolidation_transport_verification.py tests/test_consolidation_oracle_closure.py tests/test_governance_oracle_closure.py tests/test_governance_egress.py tests/test_governance_postfilter.py tests/test_record_governance.py tests/test_media_deletion_propagation.py`.

## 11. Command parity and additive Hosted v5

- [ ] 11.1 Add a red owner-authorization matrix in
  `tests/test_consolidation_surface.py` for all eleven actions. Resolve owner at
  the shared control-plane boundary before run/artifact/source/destination lookup,
  argument-dependent work, staging allocation, receipt creation, or writer
  admission; permit only resource-bounded outer schema/action decode before the
  check and prove no other field is coerced/hashed/dereferenced. Pin
  `ConsolidationOwnerContext/v1` to non-caller-selected logical
  vault, installation/generation/active-fence, canonical principal, authorization session,
  purpose, exact action, issuer/surface, issue/expiry, nonce, and verifier
  fingerprint. Hosted must derive entitlement from a validated gateway/control-
  plane assertion bound to its distinct cell context; CLI must prove actual local
  OS/vault owner plus authenticated TTY or protected OOB, never an unbound
  `local_owner()`/library default; MCP/REST use trusted session resolution. Assert
  wrong-action/vault/installation/generation/session/issuer/expiry, `cell_id ==
  vault_id`, non-owner, and unresolved calls have content-free equivalent
  outputs and zero CPU/storage/import side effects. Prove an agent under a valid
  owner connection may request deterministic reconcile/plan calculation but still
  cannot supply the separate human confirmations for apply, drift-reconciled
  rollback, or retirement.
- [ ] 11.2 Add red tests in `tests/test_consolidation_surface.py` proving the registry
  contains one `consolidate_memory` product command with exactly eleven actions,
  the normative action table's exact required/optional/forbidden fields and
  conditional `plan` render/materialize, rollback
  `nonterminal-contingency|terminal-plan`, and retire clearance/finalize branches,
  bounded UUID/ref/SHA-256/int/string types, shared envelope/errors, conservative
  write-capable annotation, and `invocation_is_read_only == true` only for `status`.
  Generated JSON Schema and runtime must implement the same closed tagged
  `oneOf`; omitted/unknown/null/duplicate/cross-action fields fail closed. All ten
  other actions require explicit operation id (CLI persists/prints any generated
  id before request), expected run revision, writer/control admission,
  idempotency, and the action-tagged stable `exomem.consolidation-terminal/v1`
  with only its normative digest/count/next-action/trusted-output keys. Generate
  an exact positive and negative fixture for every terminal row: required common
  and mutation-only fields, exact branch `artifact_digests` keys, every required
  bounded integer `counts` key, duplicate-free ordered phase-eligible subset of
  the closed `next_actions` enum, and exact required/conditional/forbidden opaque
  `trusted_outputs` keys/types. Status uses `outcome=observed`; every mutation's
  durable logical terminal uses `outcome=committed`. Required missing keys,
  extras, wrong branch, forbidden nulls, unknown next action, or wrong count/ref
  types fail generated and runtime validation. Test
  status cursor; the exact successor-context ref/digest required together on
  every shared-table product terminal (including both rollback-complete modes,
  repair targets, retirement finalize, and status-only pending-forward),
  materialize, each render transition, render-complete, approval, and eligible
  sealed-nonterminal apply/recover/verify with the exact plan-materialize versus
  contingency context kind; plan render-session/delivery/completeness
  refs; approval-token ref; and retirement-clearance/lifecycle refs as opaque
  owner/control-only outputs. Generate terminal fixtures proving the successor
  variant matches the next request, owner-detail status returns the existing
  context without mint/revision/event, summary status omits it, and protected
  seed/predecessor/publication/authority facts are unrepresentable in product
  output while the returned full-context digest remains exact. Generate the
  Cartesian negative fixture of every plan kind against every nonrow product
  terminal/phase/mode/repair target/run mode/eligibility: no plan-entry keys and
  no `plan` next action are serializable. Trusted adapters
  render plan pages to TTY/confirmation/elicitation
  without page bodies entering agent terminals, retry rows, logs, or receipts.
  Reject unknown/private
  response keys in generated and runtime schemas. Inject lost
  acknowledgement for every mutating action and conditional branch (all render
  steps and both retirement phases) and retry across a different surface;
  reserve globally by vault/installation-generation/owner/operation id, store the
  action/request digest, and prove same owner/action/operation/canonical request
  returns byte-identical canonical logical terminal `T` while the exact closed
  envelope changes only from
  `{"success":true,"data":{"delivery":"initial","terminal":T}}` to
  `{"success":true,"data":{"delivery":"replayed","terminal":T}}`.
  Generated schemas and fixtures SHALL require exactly the two top-level keys,
  exactly `delivery`/`terminal` under `data`, the two-value delivery enum, initial
  for status/first delivery, and replayed only for adoption of an existing
  mutating terminal; changed input or
  reuse for an intentional later identical action conflicts/requires a new id.
- [ ] 11.3 Add red MCP/REST/CLI tests for opaque artifact/attestation/run/token refs,
  bounded pagination, rejection/redaction of inline bytes, paths, credentials,
  verifier keys, caller identities, `approved`/`--yes`, and serialized authority.
  Prove authenticated TTY, host elicitation/out-of-band confirmation, and Hosted
  operator confirmation inject equivalent purpose-bound context without changing
  the canonical engine plan/token.
- [ ] 11.4 Add red schema/generation tests proving the only base MCP fixture addition
  is `consolidate_memory` plus explicitly enumerated bootstrap/help changes; REST,
  OpenAPI, CLI, and capability docs derive from the same entry and expose identical
  action semantics and error codes without a hand-maintained surface action list.
  Include fixed generated fixtures for the closed successor-context tagged union,
  exact plan-successor automaton object/vector, terminal trusted-output branches,
  and exact `{success,data:{delivery,terminal}}` success wrapper across all four
  surfaces; no adapter-local envelope or optional flattened delivery is allowed.
- [ ] 11.5 Extend `tests/test_hosted_agent_surface.py`, Hosted plugin/manifest tests,
  deployment-lock tests, client fixtures, and promotion guards red-first for
  additive `hosted-alpha-agent-v5`. Assert its ordered membership is byte-for-byte
  v4 membership followed by `consolidate_memory`, it has a distinct descriptor/hash
  and generated plugin/manifest fixtures, and v1-v4 membership, descriptors/hashes,
  plugins/manifests, locks, clients, and registered evidence remain byte-identical.
- [ ] 11.6 Add red Hosted selection/promotion tests proving defining/building v5 does
  not select or auto-promote it; v1-v4 cells omit/refuse consolidation; explicit v5
  deployment selection requires a closed signed private
  `HostedProfileSelection/v1` binding typed cell/vault and installation/
  generation/active-fence, profile/descriptor hash,
  release/protocol, Records reader, identity schema, consolidation run/seal/
  receipt readers, artifact-store, source-export/control-receipt verifier, and
  archive-custodian verifier readiness,
  owner-confirmation, owner-entitlement-verifier, exact-cell transport-supervisor,
  and rollback/recovery readiness, operation/validity, signature algorithm,
  signer key id, record digest, and signature. Pin its exact closed field names, framed JCS
  `exomem.hosted-profile-selection/v1` digest excluding digest/signature, and
  Ed25519 signature over
  `u32be(len("exomem.hosted-profile-selection-signature/v1"))||domain||
  u64be(32)||raw_32_byte_record_digest`, with raw 64-byte unpadded-base64url
  signature and `ed25519-sha256:<lowercase-public-key-hash>` signer id. Pin private
  `HostedProfileSelectionVerifierRecord/v1` exact schema/algorithm/key-id/public-
  key/purpose/issuer/deployment-audience/profile/status/validity/registry-
  generation fields plus revocation fields iff revoked, and explicit
  two-key rotation overlap plus cross-runtime fixed vectors. Add focused `tests/test_hosted_plugin_promotion.py` and
  `tests/test_hosted_deployment_lock_consumption.py` cases for every missing,
  stale, unsigned, expired, revoked, wrong-purpose/audience/profile, unknown-key,
  caller-made, one-field-mismatched readiness tuple, startup
  advertise/admit refusal, and explicit compatible selection. Keep private
  artifact and trusted-confirmation routes service/operator authenticated and
  absent from public command arguments.
- [ ] 11.7 Add the common owner-control authorization guard before consolidation
  coercion/existence/allocation, then register the engine once in `_PRODUCT_SPEC`,
  action validation, classifier, bootstrap/capability docs, and generated surfaces.
  Add v5 as a new immutable
  profile and wire Hosted through generic discovery plus the lifecycle owner-
  control admission lane. Validate/consume the closed profile-selection record at
  startup before advertise/admit; do not infer it from lifecycle flags, add a
  public bespoke consolidation route, modify v1-v4, or put the product command in
  the transfer intercept set.
- [ ] 11.8 Regenerate only intentional MCP schema, OpenAPI/capability/bootstrap/help,
  and new v5 descriptor/plugin/manifest/compatibility artifacts. Inspect the exact
  diff against task 0.2 baselines and reject unrelated drift.
- [ ] 11.9 Run
  `uv run python -m pytest -q tests/test_consolidation_surface.py tests/test_mcp_schema_fidelity.py tests/test_rest_registry.py tests/test_rest_api.py tests/test_cli_ops.py tests/test_bootstrap_capabilities.py tests/test_hosted_agent_surface.py tests/test_hosted_plugin_definition.py tests/test_hosted_plugin_rendering.py tests/test_hosted_release_manifest.py tests/test_hosted_plugin_promotion.py tests/test_hosted_deployment_lock_consumption.py`.

## 12. Installed-wheel, Hosted, rehearsal, and retirement E2E

- [ ] 12.1 Add a deterministic two-vault fixture builder and installed-wheel runner
  `scripts/e2e_consolidation.py`. It SHALL create canonical Sources, Evidence,
  compiled notes, Records, media/sidecars, semantic units, identities,
  history/supersession, relations/citations, review state, policies, and every C1-C8
  class only through public or test-fixture-authoring setup before server start.
  Adopt/bind each local cell through machine-owned trust, exercise same-installation
  move preservation, adopt-then-legitimate-write/current-census export, copied-
  binding and `cell_id == vault_id` collision refusal, fenced N->N+1 failover with
  fresh installation id and stale-source rejection, and create rehearsal clones
  only through the explicit new-id/clone-of operation.
- [ ] 12.2 Through a real installed stdio MCP client, run authenticated export,
  start/inventory/reconcile, fresh principal attestations, exact plan, trusted owner
  approval, seal, policy-first multi-batch apply, rebuild, positive/negative probes,
  durable routing stop/drain, normal-auth exact-cell MCP/REST/Hosted/CLI transport
  probes, routing open, restart, and persistent verification. The external `verify` control call
  may trigger in-process phase-bound probes, but the test SHALL assert no internal
  authority appears on stdio; actual content parity calls use normal authentication
  first while the exact operation's seal is phase-suspended with public routing
  stopped, then after routing opens. Assert exact destination plan,
  complete preservation/lineage, no copied source authority/derivatives, unchanged
  source/archive, content-free receipts, bounded timeouts, and no optional reasoning
  model/network dependency. Exercise the valid control-basis successor path by
  carrying each returned owner-only context from reconcile to materialize, each
  render page/ack/completion, approval and token reservation into apply, with
  unrelated physical receipt interleaving accepted. Drop each response in turn
  and recover the same durable pair only via owner-detail status; prove summary/
  non-owner status omits it. Then inject missing/mismatched/expired/consumed
  context and an unexpected/reordered/replayed same-run event and prove staleness
  before publication. Then drive every direct and repaired producer in the
  shared plan-entry table, including rollback-complete, status-only retirement-
  pending-forward-only, and retirement-finalize, and exhaustively refuse every
  nonrow product terminal/phase/mode/run-mode/eligibility combination.
- [ ] 12.3 Add an authenticated REST/CLI parity pass at every observable state and a
  complete disposable Hosted v5 lifecycle using an explicitly selected compatible
  v5 candidate plus valid signed `HostedProfileSelection/v1` and verifier record,
  including source-export/custodian, owner-entitlement, and exact-cell transport-
  supervisor readiness. Add
  unsigned/unknown-key/expired/revoked/wrong-scope and every one-field readiness
  negative while proving v1-v4 remain byte-identical. During the seal, black-box
  calls prove only the ordinary content-free sealed response; during exact-cell
  transport verification they run only under routing-stop supervision; after
  routing-open they prove persistent parity with no serialized internal authority.
  Exercise Hosted apply with another active mutation and prove control admission
  converts only itself out of the ordinary count and drains without self-deadlock;
  crash and reach exact owner-control recovery while generic dispatch remains
  sealed. For every action/conditional branch prove identical canonical `T`
  inside the exact `{success,data:{delivery,terminal}}` wrapper on all four
  surfaces: first delivery/status is `initial`, only adoption of a stored
  mutation is `replayed`, and no flattening/extra key is accepted. Prove v2
  discovery/dispatch still omits consolidation and no v3
  definition/build automatically changes the active profile or promotion state.
- [ ] 12.4 Run the full crash matrix from task 8 through installed processes, not just
  in-process exceptions, including the six receipt/journal steps from task 9 for
  every phase/effect, the seventh action-level idempotency step wherever that
  effect completes the product action, and the extra inert seed/ref reservation
  seam for every successor producer. After each kill, assert seal/routing-stop before readiness, exact
  prior/prepared/final recovery, one semantic effect, one outer `receipt/v1`
  intent with 64-hex id and one suffix-named terminal carrying closed nested
  consolidation payloads, actual physical `prev`, independently exact semantic
  parent, and journal outer-id/hash/seq plus nested-digest references. Interleave
  unrelated receipt appends and reject broken successor order. For every shared
  plan-entry-table row assert the plan intent semantic parent equals the durable
  context predecessor. Pin fixed `S`, terminal `P`, and full-context digests;
  prove terminal-before-final reconstructs context without another event or
  revision, final-before-idempotency reconstructs only `T`, and no receipt binds
  the full context. Cross every plan kind with all nonrow current/repair
  terminal kinds and assert no context or intent. Run
  `rollback_mode=nonterminal-contingency` after a sealed apply crash using only
  its returned opaque context pair, which resolves the reserved contingency
  authority/publication/journal/current predecessor server-side, then
  `rollback_mode=terminal-plan` after a completed cutover using its separately
  rendered/approved plan/token and context; prove mixed/protected body fields,
  mode-changing retries, and wrong/stale contexts conflict. Assert unchanged source, deletion-seal
  non-regression, and bounded completion with no hung client/server.
- [ ] 12.5 Run a `cloned-rehearsal` E2E bound to explicit clone identities through
  newly generated active vault/installation ids and immutable clone-of
  vault/installation/snapshot lineage through apply, in-process pre-unseal probes,
  post-unseal black-box transport parity, selected crash seams, and full rollback.
  Then prove its
  snapshot/attestation/plan/token/JTI/authority cannot authorize a `real-cutover`
  fixture; generate fresh real-mode state and require a second owner confirmation.
- [ ] 12.6 After the temporary real-mode fixture verifies, prove `retire-source`
  remains refused until a third retirement-specific confirmation, current
  destination verification, available verified preimage, unchanged authenticated
  source checkpoint, rehearsal-with-rollback proof, exact archive disposition, and
  retained-versus-irrecoverable provenance statement all exist. Exercise retain,
  trusted-custodian transfer, and external-destruction declarations; assert C1
  owner-only mappings remain. For custodian transfer, use exact signed
  `archive-custody-receipt/v1`/`ArchiveCustodianVerifierRecord/v1` fixed vectors
  and independently revalidate signer/custodian/domain/terms/transfer/artifact/
  manifest/census/validity at retirement plan and clearance. Exercise `pre-cutover-reversible` with retained
  source/archive and `forward-only` with a separately verified forward snapshot,
  a pending pre-cutover rollback fence installed before clearance and made
  permanent only by finalize, and a surviving-copy ledger for
  every imported byte/provenance bundle. Assert a zero-copy proposal fails before
  clearance and the fixture source/archive remain undeleted because actual
  disposition is external.
- [ ] 12.7 Consume the content-free retirement clearance under the source lifetime/
  fencing boundary, then inject expiry, source/checkpoint drift, archive-
  disposition/digest drift, destination verification/recovery/no-loss drift,
  operation/JTI replay, and stale fence. Only the exact consumption returns its
  idempotent committed logical terminal with the exact replay envelope
  `{"success":true,"data":{"delivery":"replayed","terminal":T}}`; every
  mismatch performs nothing. Revalidate custody again at consume, rollback-plan,
  and rollback commit and inject expiry/revocation/terms/artifact drift between
  gates. Verify clearance issuance,
  consume, external completion attestation, and destination finalize are four
  distinct evidence transitions and no Exomem path performs the external deletion.
- [ ] 12.8 Add documentation/bootstrap/result assertions that direct filesystem or
  block-device access, manual copy/paste, direct artifact/object-store access, and
  upload to an external model outside Exomem are outside the enforcement claim.
  Do not turn this disclaimer into a claim that Exomem can detect those bypasses.
- [ ] 12.9 Run
  `uv build && uv run --frozen python scripts/e2e_consolidation.py --budget-seconds 600 --request-timeout 20`, then the existing
  `uv run --frozen python scripts/e2e_product_loop.py --budget-seconds 240 --request-timeout 20` and
  `uv run --frozen python scripts/e2e_http_server.py`.

## 13. Documentation, compatibility, and release gates

- [ ] 13.1 Update only generic product/bootstrap/capability/operator documentation
  needed to discover consolidation, its owner-inclusive maintenance seal, fresh
  destination principal requirement, three distinct confirmations, source-retention
  obligation, v5 Hosted selection, and Exomem-mediated boundary. Keep scaffold
  examples generic and do not mention a personal, client, or operator vault.
- [ ] 13.2 Add a software rollback compatibility test: new starts may be disabled,
  but a deployment may not remove a reader/recovery implementation needed by any
  active sealed/nonterminal run. Unknown newer run/receipt schema fails closed and
  is preserved; no downgrade regains readiness by deleting seal or evidence state.
- [ ] 13.3 Run the focused no-model consolidation suite exactly:
  `EXOMEM_DISABLE_EMBEDDINGS=1 EXOMEM_DISABLE_MEDIA_EXTRACTION=1 uv run python -m pytest -q tests/test_consolidation_cell_identity.py tests/test_consolidation_intake.py tests/test_consolidation_run_state.py tests/test_consolidation_reconciliation.py tests/test_consolidation_policy.py tests/test_consolidation_plan.py tests/test_consolidation_seal.py tests/test_consolidation_control_admission.py tests/test_consolidation_oracle_closure.py tests/test_consolidation_preimage.py tests/test_consolidation_saga.py tests/test_consolidation_recovery.py tests/test_consolidation_rollback_plan.py tests/test_consolidation_retirement.py tests/test_consolidation_receipts.py tests/test_consolidation_verification.py tests/test_consolidation_transport_verification.py tests/test_consolidation_surface.py`.
- [ ] 13.4 Run the affected regression suites exactly:
  `EXOMEM_DISABLE_EMBEDDINGS=1 EXOMEM_DISABLE_MEDIA_EXTRACTION=1 uv run python -m pytest -q tests/test_hosted_portability.py tests/test_hosted_restore_candidate.py tests/test_hosted_lifecycle_regressions.py tests/test_writer_lease.py tests/test_vault.py tests/test_governance_policy.py tests/test_governance_decisions.py tests/test_governance_membership.py tests/test_governance_egress.py tests/test_governance_postfilter.py tests/test_governance_receipts.py tests/test_governance_tokens.py tests/test_govern_memory_tool.py tests/test_authorization_session_binding.py tests/test_reserved_admin_paths.py tests/test_get_payload.py tests/test_governance_oracle_closure.py tests/test_record_governance.py tests/test_media_processing.py tests/test_media_deletion_propagation.py tests/test_mcp_schema_fidelity.py tests/test_rest_registry.py tests/test_cli_ops.py tests/test_bootstrap_capabilities.py tests/test_hosted_agent_surface.py tests/test_hosted_plugin_promotion.py tests/test_hosted_deployment_lock_consumption.py`.
- [ ] 13.5 Run the lean full suite and performance gates with a writable writer-state
  directory and model/media extras disabled:
  `EXOMEM_WRITER_LEASE_STATE_DIR=/tmp/exomem-consolidation-writer-state PYTHONPATH=src EXOMEM_DISABLE_EMBEDDINGS=1 EXOMEM_DISABLE_MEDIA_EXTRACTION=1 uv run --frozen python -m pytest -q`, followed by
  `uv run --frozen python -m pytest -q tests/test_latency_gate.py tests/test_governance_overhead.py` and
  `uv run --frozen python scripts/semantic_write_latency.py --check`. Remove only the
  task-specific temporary directory after verifying its exact path and no live run.
- [ ] 13.6 Run `uv lock --check`, `uvx ruff check . --select F`, full Ruff over every
  new/changed consolidation source/test/script with `--select E,F,I,B,UP,BLE`,
  targeted `uvx mypy --follow-imports skip --ignore-missing-imports --check-untyped-defs`
  over the new consolidation core, `uv run python -m pytest -q
  tests/test_scaffold_no_leak.py`, `uv run --frozen python
  scripts/generate-capabilities.py --check`, `git diff --check`, and `openspec
  validate add-governed-vault-consolidation --strict`.
- [ ] 13.7 Run the optional real embedding/reranker and media/CLIP verification lanes
  on an equipped host after the no-model gate. Record unavailable optional lanes
  explicitly; optional dependency absence may not waive keyword, graph, raw-read,
  seal, receipt, or oracle-closure coverage.

## 14. Independent security review and evidence-gated closeout

- [ ] 14.1 Have an independent security reviewer inspect the exact implementation diff
  and attack local identity adoption/rebind, copied bindings, installation
  collisions, `cell_id`/`vault_id` aliasing, clone/failover generation/fence
  split-brain, same source/destination, archive self-authentication, Ed25519
  canonicalization/key rotation/revocation/custody, trust-key injection,
  non-owner initiation/resource DoS and forged owner contexts,
  live-source omission,
  staging/index leakage, reserved-path aliases, C1-C8 precedence/no-loss, copied
  authority, attestation/session crossover, canonicalization/hash ambiguity,
  immutable control-basis/allowed-successor self-staleness, unexpected same-run
  event injection, incomplete trusted-plan rendering, approval forgery/replay,
  nonterminal-versus-terminal rollback branch confusion, action-schema and
  cross-surface operation-id confusion, owner seal bypass, control-admission self-
  deadlock, deletion-seal reopening, serializable internal authority, policy-after-
  content ordering, cross-store causality gaps/mixed crash states, outer
  `receipt/v1` versus nested consolidation-payload compatibility, intent/terminal
  id grammar, physical `prev` versus semantic-parent confusion, receipt
  deletion/rewind, pre-unseal authority serialization, exact-cell transport/
  routing race, post-unseal rollback overwrite, post-retirement zero-copy loss,
  C1 provenance loss, custody-receipt signer/domain/retention/artifact drift,
  archive-disposition/clearance-consume replay ambiguity,
  source-retirement conflation, v5 selection signature/verifier/readiness and Hosted v1-v4
  drift, and path/rank/graph/count/error/timing oracles.
- [ ] 14.2 Record every review finding with severity and exact file/test evidence. For
  each accepted finding, add a reproducing red test before the smallest fix, rerun
  the focused and affected regression gates, and require the same reviewer to
  recheck the amended exact diff and explicitly close or retain the blocker.
- [ ] 14.3 Have a separate independent verifier run tasks 13.3-13.7 and the installed
  stdio, REST, CLI, and explicitly selected Hosted v5 E2E from a clean environment.
  The verifier SHALL inspect v1-v4 byte-identity baselines, kill/restart seams,
  same-input negative pairs, receipt plaintext scans, rehearsal rollback, distinct
  confirmation gates, and source/archive/destination final fingerprints rather than
  accepting self-reported terminals.
- [ ] 14.4 Do not claim the capability shipped until review/recheck, verification, all
  required CI jobs, generated-artifact checks, strict OpenSpec validation, and merge
  evidence are green. Only then sync the seven delta specs to canonical specs and
  archive this change under the repository workflow.
- [ ] 14.5 Keep the real consolidation operational gate separate after product
  closeout: first perform and review a clone-bound rehearsal including negative
  disclosure and rollback; then generate and separately confirm a fresh real exact
  cutover plan; keep the source recoverable; and require a third retirement-specific
  confirmation before any source-side operator action. Product CI or merged code is
  never authority for those real operations.
