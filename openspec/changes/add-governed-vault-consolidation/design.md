## Context

Exomem already has the pieces needed to build a safe consolidation path, but no
single product operation composes them. Hosted portability can produce and
verify a quiesced content-addressed export. Governance can preview and activate
exact reviewed policy through a receipt-first journal. The release gate covers
the product surfaces. Adoption Studio supplies a useful durable-run and review
model, but it deliberately rejects already-governed material and converts legacy
files into imported Sources. It is therefore the wrong merge engine for two
managed Exomems.

The change combines two already-managed vaults into one existing destination.
That is a high-risk in-place migration: identities and paths can collide,
source policy identities are not destination authority, policy and content
cannot become visible in the wrong order, and a crash can leave more than one
canonical file published. A multi-file filesystem write is not power-loss
atomic, so the design must be an explicit resumable saga rather than claim a
transaction the storage substrate cannot provide.

The prerequisite change `harden-governance-for-consolidation` must be merged and
verified before implementation of this change is accepted. In particular,
per-scope grants, prospective compile conflict binding, raw-read projection,
reserved administration paths, principal-bound authorization sessions, and
remaining existence oracles are assumptions of this design, not workarounds to
be reimplemented inside the consolidation engine.

The durable product decision is one broadly connected destination with a
separate deterministic release plane. The agent interprets the user's intent
and proposes reconciliation and policy. Exomem measures, validates, journals,
applies, verifies, and refuses. No server-side reasoning model is added.

## Goals / Non-Goals

**Goals:**

- Admit only an authenticated, quiesced, content-addressed source snapshot and
  bind it immutably to the destination run.
- Inventory both managed vaults without exposing source bytes to ordinary
  destination recall or mutating either corpus.
- Classify every source object through one deterministic reconciliation matrix,
  preserve append-only artifacts and lineage, and require owner decisions for
  every non-byte-identical or authority conflict.
- Produce one exact content-plus-policy preview and a single-use approval bound
  to both snapshots, fresh destination principal attestations, every conflict
  decision, every planned write, and the verification/rollback plans.
- Apply through a destination-wide, owner-inclusive seal, restrictive policy
  first, journaled content batches, derived rebuild, positive/negative probes,
  and unseal only after all mandatory verification passes.
- Recover after interruption without replaying semantic decisions, and make
  pre-publication abort, post-publication rollback, and source retirement three
  distinct operations with distinct approval and evidence.
- Expose one deterministic contract through MCP, REST, CLI, OpenAPI, and Hosted.

**Non-Goals:**

- Extending Adoption Studio into a managed-vault merge mode or changing its
  existing lifecycle.
- A live remote-MCP crawl of the source vault. Source release policy could omit
  objects without the destination knowing they existed, so it cannot form a
  complete migration inventory.
- Copying source policy documents, source audience identifiers, credentials,
  grants, tokens, authorization sessions, runtime bindings, or derived indexes
  into destination authority.
- A server-side LLM, automatic conflict judgment, semantic similarity merge, or
  inferred disclosure policy.
- Treating direct filesystem access, manual copy/paste, or uploads to an
  external model outside Exomem as governed by the release plane.
- Touching the real source or destination as part of shipping the capability.
  Clone rehearsal, real cutover, and source retirement are later operational
  runs with separate approvals.

## Decisions

### 1. Add `consolidate_memory`; do not make Adoption Studio conditional

`consolidate_memory` is a new multiplexed product command with actions `start`,
`status`, `reconcile`, `plan`, `approve`, `apply`, `verify`, `recover`, `abort`,
`rollback`, and `retire-source`. One command-registry entry generates MCP,
REST, CLI, OpenAPI, and Hosted exposure. Only `status` is read-only; every other
action creates or changes durable run/evidence state or migration state and
uses writer admission.

All eleven actions are owner-authorized control-plane operations. Authorization
is resolved before run, artifact, source, or destination existence is looked up,
so a non-owner cannot initiate work, consume storage/CPU, enumerate runs, or use
consolidation as an import or denial-of-service path. An agent inside an
authenticated owner connection may compute an inventory and propose structured
reconciliation or policy, but this does not grant it owner identity and does not
replace the separate human confirmations for plan approval, a drift-reconciled
rollback, or source retirement.

Owner authorization is a verified `ConsolidationOwnerContext/v1`, not a
`local_owner()` default. It binds logical vault id, installation id/generation
and active-fence digest,
canonical principal, authorization session, purpose=`vault-consolidation`,
allowed action, issuer/surface family, issued/expiry, nonce, and keyed verifier
fingerprint. It is resolved and verified before body-dependent coercion,
existence lookup, artifact access, idempotency reservation, or expensive work;
only bounded decoding of outer `schema`/`action` may precede it so the adapter
can select the required purpose. The body cannot select or widen any field.
Hosted accepts only a validated
gateway/control-plane owner-entitlement assertion bound to its trusted cell
context. CLI requires the actual authenticated local OS/vault owner plus an
interactive TTY or protected out-of-band capability; an in-process/library call
without explicit verified owner context fails closed. MCP/REST use their trusted
principal/session resolver. Malformed, expired, wrong-action, cross-vault,
cross-installation, cross-session, or cross-issuer contexts are equivalent
content-free denials.

Hosted exposure is additive rather than a mutation of a closed surface. A new
`hosted-alpha-agent-v5` profile contains the exact v4 command sequence plus
`consolidate_memory`; v1 through v4 descriptors, hashes, generated plugin/manifest
fixtures, locks, clients, and registered evidence remain byte-identical. The v5
profile has its own descriptor/hash, generated fixtures, compatibility and
promotion evidence, and explicit deployment selection. A cell not explicitly
configured for v5 does not advertise or admit consolidation, and creation of v5
does not auto-promote any deployment. Private artifact delivery and trusted
confirmation remain operator/control-plane seams beneath the registered v5
product command rather than public request fields.

V5 selection is authorized only by a closed private
`HostedProfileSelection/v1` record from the deployment/control plane. It binds
typed cell and logical vault identities, installation id/generation/active fence,
profile id and descriptor hash, release and
protocol, Records reader version, identity-schema version, consolidation
run/seal/receipt reader versions, artifact-store readiness fingerprint,
source-export/control-receipt verifier readiness fingerprint,
archive-custodian verifier readiness fingerprint, owner-confirmation
readiness fingerprint, owner-entitlement-verifier readiness fingerprint,
exact-cell transport-supervisor readiness fingerprint, rollback/recovery closure
digest, operation id,
issue/expiry, and control-plane signature/record digest. Startup validates the
whole tuple against the running image and private dependencies before advertising
or admitting v5. Missing, unknown, partial, stale, incompatible, or caller-made
selection keeps existing explicitly selected v1 through v4 behavior byte-identical; it
never infers v5 from a lifecycle flag or capability presence and never promotes
it automatically.

The closed field names are `schema`, `cell_id`, `vault_id`, `installation_id`,
`installation_generation`, `active_fence_digest`, `profile_id`,
`descriptor_digest`, `release_digest`, `protocol_version`,
`records_reader_version`, `identity_schema_version`,
`consolidation_run_reader_version`, `consolidation_seal_reader_version`,
`consolidation_receipt_reader_version`, `artifact_store_readiness_digest`,
`source_export_verifier_readiness_digest`,
`archive_custodian_verifier_readiness_digest`,
`owner_confirmation_readiness_digest`,
`owner_entitlement_verifier_readiness_digest`,
`transport_supervisor_readiness_digest`, `rollback_recovery_closure_digest`,
`operation_id`, `issued_at`, `expires_at`, `signature_algorithm`,
`signer_key_id`, `record_digest`, and `signature`.
`record_digest` is SHA-256 over the common framed JCS closed object excluding
`record_digest`/`signature`, with ASCII domain
`exomem.hosted-profile-selection/v1`. `signature_algorithm` is the literal
`Ed25519`; `signer_key_id` is `ed25519-sha256:` plus lowercase SHA-256 hex of the
raw 32-byte public key. The signature covers
`u32be(len("exomem.hosted-profile-selection-signature/v1")) ||
"exomem.hosted-profile-selection-signature/v1" || u64be(32) ||
raw_32_byte_record_digest` and is a raw 64-byte Ed25519 signature encoded as
unpadded base64url.

Private `HostedProfileSelectionVerifierRecord/v1` trust records contain exactly
`schema`, `algorithm`, `key_id`, `public_key`, `purpose`, `issuer_id_digest`,
`deployment_audience_digest`, `profile_id`, `status`, `not_before`, `not_after`,
`registry_generation`, and `revoked_at`/`revocation_reason_digest` exactly for
revoked records. They bind the raw 32-byte unpadded-base64url Ed25519 public key
and derived key id, purpose `hosted-profile-selection`, issuer/control-plane
identity, deployment audience, allowed `hosted-alpha-agent-v5` profile, status
`active|inactive|revoked`, bounded integer registry generation, and ordered
validity interval. Startup verifies record
validity at the selection issue time and current startup time. Rotation permits
only an explicitly bounded two-key overlap; unknown, inactive, premature,
expired, revoked, wrong-purpose/issuer/audience/profile, or caller-supplied keys
fail. Fixed signing/rotation/revocation vectors are shared across runtimes.
Unknown fields, a mismatched digest/signature, or a non-v5 profile in this
schema fails closed.

The command accepts opaque artifact references and structured facts, never an
inline archive. Reasoning remains agent-side: the agent can propose conflict
resolutions, policy documents, explanations, and representative probes, but the
engine accepts only finite, validated operations and exact fingerprints.

The existing actions carry explicit subcontracts rather than hidden routes.
`plan` requires `plan_kind=cutover|rollback|retirement` and
`operation=materialize|render`; rendering pages the exact stored plan and records
trusted owner-session coverage. `approve` requires the same plan kind and exact
stored digest and consumes a trusted rendering-completeness capability. `apply`
accepts only a cutover token. `rollback_mode=terminal-plan` accepts only a
separately reviewed rollback plan/token, while
`rollback_mode=nonterminal-contingency` executes only the contingency and
durable reservation already authorized by the original still-nonterminal apply;
it accepts no rollback-plan token.
`retire-source(phase=clearance)` accepts only a retirement token and reserves its
JTI into one retirement-lifecycle journal; `phase=finalize` references that
exact journal and plan rather than consuming the token again, verifies the
authenticated disposition-completion proof, and converts any already-active
pending fence into its permanent post-retirement rollback rule. `recover`
resumes only a recorded operation journal; it is not an
alternate unreviewed plan path.

The generated command schema is a closed discriminated union
`exomem.consolidate-memory-request/v1`, not one flat bag of optional fields. All
variants require `schema` and `action`; all strings are NFC and bounded to 512
UTF-8 bytes unless a smaller bound is stated; ids are canonical lowercase UUIDv4
strings; refs are opaque strings and never paths; unknown, duplicate, null, or
cross-action fields are forbidden. Every mutation requires a caller-visible
`operation_id`; MCP/REST/Hosted require it explicitly, while CLI may generate a
cryptographically random UUIDv4 only if it persists and prints it before the
first request so retry can reuse it. An unkeyed mutation is refused. Every
existing-run mutation requires `run_id` and exact `expected_run_revision`
(integer 0..2^53-1); `start` requires revision 0.

Request `operation_id`, `run_id`, and `*_operation_id` fields are lowercase UUIDv4; every
`*_digest` and `expected_intent_event_id` is exactly 64 lowercase hexadecimal
SHA-256 characters. A `successor_context_ref` is an opaque owner-control
reference and its `successor_context_digest` is exactly 64 lowercase
hexadecimal; the request never supplies the protected predecessor, basis, or
contingency facts behind it. Opaque refs/cursors/render-session
refs are 1..512 UTF-8 bytes, contain no NUL, and are never path-interpreted.
Revisions and page ordinals are JSON integers (not strings/floats), with page
ordinals 0..2^31-1. The enum spellings in the table are closed.

| Action | Required action fields | Optional action fields | Forbidden notes |
|---|---|---|---|
| `start` | `operation_id`, `expected_run_revision=0`, `run_mode` (`cloned-rehearsal`, `real-cutover`), `source_artifact_ref`, `source_attestation_ref` | `clone_binding_ref` only for rehearsal | `run_id`, policy/content decisions, approval/token fields |
| `status` | `run_id` | `expected_run_revision`, `detail` (`summary`, `owner-detail`; default `summary`), `cursor`, `limit` (1..200, default 50) | `operation_id` and every mutation field |
| `reconcile` | `operation_id`, `run_id`, `expected_run_revision`, `expected_inventory_digest`, `decision_set_ref`, `decision_set_digest` | none | inline bodies/free-form decisions |
| `plan` | `operation_id`, `run_id`, `expected_run_revision`, `plan_kind` (`cutover`, `rollback`, `retirement`), `operation` (`materialize`, `render`), `successor_context_ref`, `successor_context_digest` | materialize: one closed kind-options object; render: `plan_digest`, `render_step` (`begin`, `page`, `acknowledge`, `complete`), `page_ordinal` (0..2^31-1), `render_session_ref`, `acknowledged_page_digest` according to step | multiple kind-options, caller plan bytes or section/page definitions, explicit predecessor/basis fields |
| `approve` | `operation_id`, `run_id`, `expected_run_revision`, `plan_kind`, `plan_digest`, `rendering_completeness_ref`, `rendering_completeness_digest`, `successor_context_ref`, `successor_context_digest` | none | caller display/impact summary, boolean approval, explicit predecessor fields |
| `apply` | `operation_id`, `run_id`, `expected_run_revision`, `cutover_plan_digest`, `approval_token_ref`, `approval_token_digest`, `successor_context_ref`, `successor_context_digest` | none | non-cutover token, resume flag, changed retry input, explicit predecessor fields |
| `verify` | `operation_id`, `run_id`, `expected_run_revision`, `verification_kind` (`in-process`, `transport`), `expected_plan_digest`, `expected_verification_basis_digest` | none | serialized internal authority, caller-selected principal |
| `recover` | `operation_id`, `run_id`, `expected_run_revision`, `expected_journal_digest` | `expected_intent_event_id` | new semantic decisions, changed plan/token/artifact |
| `abort` | `operation_id`, `run_id`, `expected_run_revision`, `expected_journal_digest`, `reason_code` (`owner-cancelled`, `preimage-unavailable`, `verification-failed`, `maintenance-window-expired`) | `reason` (0..500 UTF-8 bytes, owner-only) | invocation at/after publication boundary |
| `rollback` | common: `operation_id`, `run_id`, `expected_run_revision`, `rollback_mode`, `successor_context_ref`, `successor_context_digest`; nonterminal: no additional fields; terminal: `rollback_plan_digest`, `rollback_token_ref`, `rollback_token_digest` | none | nonterminal forbids original-apply/journal/publication/contingency authority fields plus rollback plan/token/implicit target; terminal forbids original-apply/contingency fields; both forbid explicit predecessor fields, resume flag, and other-kind token |
| `retire-source` | clearance: `operation_id`, `run_id`, `expected_run_revision`, `phase=clearance`, `retirement_plan_digest`, `retirement_token_ref`, `retirement_token_digest`, `successor_context_ref`, `successor_context_digest`; finalize: `operation_id`, `run_id`, `expected_run_revision`, `phase=finalize`, `retirement_plan_digest`, `retirement_lifecycle_ref`, `completion_attestation_ref`, `completion_attestation_digest` | none | clearance forbids completion/lifecycle and explicit predecessor fields; finalize forbids token/successor-context fields; both forbid destructive source bytes/credentials and reusable report semantics |

For plan materialization, exactly the options object matching `plan_kind` is
required and all render fields are forbidden. For rendering, all kind-options
objects are forbidden: `begin` requires only `plan_digest` and returns the
trusted render-session ref; `page` additionally requires session ref and page
ordinal; `acknowledge` also requires that page's digest; `complete` requires
only plan digest/session ref and forbids page fields. Each call has its own
operation id/revision, while the render session fixes owner/session/surface and
stored plan. Every transition that needs protected causal input requires the
opaque `successor_context_ref` plus its digest instead of caller-supplied basis,
predecessor, publication-state, or contingency-authority fields.

The referenced owner-only object is the closed
`exomem.consolidation-successor-context/v1` tagged union. Every branch has
exactly `schema`, `context_kind`, `run_id`, `run_revision`,
`destination_binding_digest`, `owner_binding_digest`, `basis_digest`,
`context_seed_digest`,
`predecessor_event_id`, `predecessor_payload_digest`, `successor_action`,
`successor_variant`, `issued_at`, `expires_at`, `nonce`, and a closed `facts`
object. The predecessor id is `<64-lowercase-hex>:committed`; all digests are 64
lowercase hex; timestamps use the common RFC3339-millisecond rules;
`run_revision` is 0..2^53-1; and `issued_at < expires_at`. The exact branches are:

| `context_kind` | `successor_action` / `successor_variant` | Exact `facts` fields |
|---|---|---|
| `plan-materialize` | `plan` / `materialize` | `eligible_plan_kinds`, `plan_input_basis_digest` |
| `render-begin` | `plan` / `render-begin` | `plan_kind`, `plan_digest` |
| `render-page` | `plan` / `render-page` | `plan_kind`, `plan_digest`, `render_session_digest`, `page_ordinal` |
| `render-acknowledge` | `plan` / `render-acknowledge` | `plan_kind`, `plan_digest`, `render_session_digest`, `page_ordinal`, `page_digest` |
| `render-complete` | `plan` / `render-complete` | `plan_kind`, `plan_digest`, `render_session_digest` |
| `approve` | `approve` / exactly the `plan_kind` value | `plan_kind`, `plan_digest`, `rendering_completeness_digest` |
| `apply` | `apply` / `cutover` | `cutover_plan_digest`, `approval_token_digest` |
| `rollback-terminal-plan` | `rollback` / `terminal-plan` | `rollback_plan_digest`, `rollback_token_digest` |
| `retire-source-clearance` | `retire-source` / `clearance` | `retirement_plan_digest`, `retirement_token_digest` |
| `rollback-nonterminal-contingency` | `rollback` / `nonterminal-contingency` | `original_apply_operation_id`, `original_apply_journal_digest`, `cutover_plan_digest`, `rollback_contingency_digest`, `publication_state_digest`, `contingency_authority_ref`, `contingency_authority_digest`, `recovery_window_deadline` |

`successor_context_digest` is SHA-256 over the common frame with exact ASCII
domain `exomem.consolidation-successor-context/v1` and the closed JCS object;
the opaque ref and digest are not fields inside those bytes. The full object is
derived only after its committed predecessor under the seed contract below;
resolution recomputes `context_seed_digest`, then verifies destination/owner
binding, run/revision, unexpired context, current committed predecessor/payload
digest, requested action/variant, basis, and every request-visible plan/session/
page/token digest against `facts`. The nonterminal rollback branch additionally
dereferences its protected contingency authority and verifies the original
apply journal, current publication state, and recovery window. The context is a
one-step state witness, not bearer authority: the first operation reservation
binds its ref/digest; only that byte-identical operation may replay, and any
other same-run successor makes it stale. A request cannot submit the context
object, its protected facts, or a replacement predecessor.

`eligible_plan_kinds` is a nonempty duplicate-free array in fixed subset order
`cutover`, `rollback`, `retirement`; other enums and digest/id/ref/timestamp
values use the closed request bounds above. `plan_input_basis_digest` covers the
server's current immutable eligible inputs. The selected kind and closed
options enter materialization's request and control basis, so reconciliation
does not guess future caller-selected kind/options.

`destination_binding_digest` is exactly the authenticated identity/root-binding
fingerprint in the destination snapshot. `owner_binding_digest` is framed-JCS
SHA-256 under ASCII domain
`exomem.consolidation-successor-owner-binding/v1` over a closed object with
exactly `schema`, `vault_id`, `installation_id`, `generation`,
`active_fence_digest`, `principal_digest`, and `purpose=vault-consolidation`, all derived from
the validated owner context rather than the body; `schema` equals that same
versioned domain string. `basis_digest` equals
`plan_input_basis_digest` for `plan-materialize`, the relevant stored plan's
`control_basis_digest` for every render/approve/apply/terminal-rollback/
retirement-clearance context, and `original_apply_journal_digest` for
`rollback-nonterminal-contingency`. These three binding digests and
`context_seed_digest` are mandatory and never nullable. The stable owner binding
intentionally omits session and surface so a newly authenticated equivalent
adapter may continue or retry;
render-session facts separately require the one trusted session/surface during
page coverage.

The configured `successor_context_ttl_ms` is an integer 1..86,400,000.
`plan-materialize.expires_at` is exactly `9999-12-31T23:59:59.999Z`: it is a
non-bearer current-state witness revalidated under fresh owner authority, so a
long-lived allowed state—including a pending-forward checkpoint—cannot become
unreachable merely by time. Every stored-plan review or approved-executor
context expires at the earlier of `issued_at + ttl` and the bound plan/
completeness/token deadline; the nonterminal-contingency context expires at the
earlier of that value and its recovery-window deadline. Status never extends a
context. After a non-contingency child context expires, no action may continue
it; a new plan-entry pair can arise only from a later product terminal
explicitly listed in the table, and status cannot mint one.

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

Only the exact plan-entry table installs or returns a `plan-materialize`
context with its exact nonempty eligible-kind set. Every plan materialize/render
terminal, render-complete, and approve persists the exact next review/executor
context before returning its opaque pair. A nonrow plan-entry terminal omits the
pair and cannot advertise `plan`. `status` with
`detail=owner-detail` returns the already-durable current pair, including the
nonterminal-contingency context when eligible; it never mints, refreshes, or
advances one. Apply, terminal-plan rollback, and retirement clearance resolve
the context from the matching approval, reserve the JTI, and persist the
resulting token-reservation committed event as the executor's separate current
predecessor. Retirement clearance consumes the approval JTI into the returned
retirement-lifecycle ref; finalize uses that ref and cannot submit the token
again. The nonterminal contingency reservation is bound to the cutover plan,
run, original apply operation, and recovery window and is not a serializable
`ConsolidationAuthority` or a new rollback token.
Apply/rollback recovery uses the identical original tagged request and
operation id; there is no body `resume` flag whose addition could change the
canonical request digest.

Materialize uses exactly one closed options object. `cutover_options` requires
`expected_reconciliation_digest`, `expected_policy_bundle_digest`,
`expected_principal_attestation_set_digest`, `expected_verification_plan_digest`,
`expected_rollback_contingency_digest`, `expected_source_retention_digest`, and
`expected_control_basis_digest`, plus `expected_rehearsal_proof_digest` only for
real cutover. `rollback_options` requires target kind
(`pre-cutover|post-cutover-forward`), target snapshot ref/digest, expected current
census, treatment-set ref/digest, surviving-copy-ledger digest, and expected
retirement-state digest. `retirement_options` requires archive disposition
(`retain|transfer|external-destruction`), archive ref/digest/terms, rollback mode
(`pre-cutover-reversible|forward-only`), source-retention-proof ref/digest,
surviving-copy-ledger digest, and retained/irrecoverable statement digest;
  transfer alone adds `custodian_receipt_ref`/`custodian_receipt_digest` and
  forward-only alone adds
forward-snapshot ref/digest. Unknown/inapplicable fields are forbidden, and a
current source proof never counts as a surviving post-destruction copy.

The three kind-specific materialize objects are closed: cutover options bind the
expected complete reconciliation/policy/verification/retention digests;
rollback options bind target, current census, treatment-set ref/digest, and
surviving-copy ledger digest; retirement options bind archive disposition and
artifact/authenticated-custodian receipt, post-retirement rollback mode,
forward-snapshot proof
when required, and retained-versus-irrecoverable statement digest. Conditional
requirements are generated JSON Schema `oneOf` branches and the same runtime
tagged-union validator; adapters cannot weaken them.

Every successful response is
`exomem.consolidation-terminal/v1`: `schema`, `action`, `outcome`
(`committed|observed`), `run_id`, `run_revision`, `phase`,
`operation_id` for mutations, canonical `request_digest`, `prior_state_digest`
and `final_state_digest` for mutations, bounded content-free `counts`, exact
action-allowed `artifact_digests`, and closed `next_actions`. Status uses
`observed`; every mutation uses `committed` even when an adapter is replaying a
lost acknowledgement. Status omits operation/prior/final fields. Every MCP,
REST, CLI, and Hosted success is the exact closed canonical envelope
`{"success":true,"data":{"delivery":D,"terminal":T}}`: `D` is exactly the
enum string `initial` or `replayed`, `T` is the logical terminal, the top level
has exactly `success` and `data`, and `data` has exactly `delivery` and
`terminal`. The idempotency store persists only canonical JCS bytes for `T`;
the adapter constructs the envelope after lookup. First delivery and every
status response use `initial`; only a mutating reservation returning its stored
terminal uses `replayed`. A replay changes only `D` and returns byte-identical
canonical `T`. Errors use the existing
shared failure envelope
and stable code/remediation with no private facts.

The terminal is also an action-tagged generated `oneOf`: its digest keys are
closed to source/destination snapshot (`start`), inventory/reconciliation/map,
plan/render/completeness, approval plan/token, cutover terminal/post-census,
verification basis/terminal, journal/recovery, abort prior/terminal, rollback
nonterminal-contingency or terminal-plan basis/target/terminal, or retirement
plan/clearance/lifecycle/completion/finalize as applicable. Every branch has an
exact required `artifact_digests` key set, exact required integer `counts` key
set, an always-present ordered/deduplicated `next_actions` array drawn from its
closed branch enum, and an always-present exact `trusted_outputs` object. The
command-surface delta enumerates those sets and their conditional requiredness;
generated schema, fixtures, runtime serialization, and retry storage share that
table. Paths,
bodies, raw principals, private refs, and token bytes are not representable, and
a retry returns the same logical terminal.

The action-tagged terminal's `trusted_outputs` is `{}` except for the exact
successor-context pair, status continuation, plan render-session/delivery/
completeness refs, approval token ref, and retirement clearance/lifecycle refs
enumerated by that table. Every listed plan-entry product terminal, plan
materialize and every render step, render-complete, approve, and an eligible
nonterminal apply/recovery/verification terminal
return `successor_context_ref` plus `successor_context_digest`; owner-detail
status returns that already-durable pair whenever one is current. They are opaque bounded
values delivered only to the authenticated owner/control adapter and never to
receipts. A render-delivery ref is dereferenced only by the trusted CLI pager,
Hosted confirmation route, or MCP/REST host elicitation/OOB seam to show the
exact stored page to the human; plan plaintext never enters the agent-facing
logical terminal, retry row, receipt, or log. Page/ref digests in the terminal
make replay and acknowledgement exact.

Cross-surface reservation identity is logical vault id + installation
id/generation + canonical owner principal + operation id. The reserved record
then binds action plus canonical request digest over the tagged request excluding
transport wrappers and injected owner context; the stored identity separately
binds the verified owner. Lost acknowledgement
may replay across MCP/REST/CLI/Hosted when a newly verified context resolves the
same owner and request bytes match. Same operation id with changed action,
request, plan/token, destination binding, or owner is `OPERATION_CONFLICT`;
intentional identical later work uses a new operation id.

Alternative: add `managed=true` to `adoption_studio`. Rejected because its
`ALREADY_GOVERNED` refusal, copy-as-Source semantics, original-file model, and
partial-apply lifecycle are load-bearing safety properties. Making each
conditional would weaken both products and still would not supply a policy-bound
sealed cutover.

### 2. Authenticate the archive outside its self-asserted manifest

The source remains quiesced while `hosted_portability` builds the existing
versioned archive and verifies every file digest. Consolidation additionally
requires either:

1. an authenticated delivery receipt from a trusted source/control-plane
   channel whose issuer trust contract is equally exact; or
2. a detached Ed25519 `source-export-attestation/v1`.

Both bind the source logical vault identity, source installation id/generation
and active-fence digest, authenticated identity/root-binding fingerprint, export
operation, quiescence checkpoint, archive SHA-256, manifest SHA-256,
canonical source-census SHA-256, issue/expiry times, and signer key id. These
claims are outside the unsigned archive, so an attacker cannot recompute an
archive and its manifest and thereby invent source authority. For Ed25519, the
source signs
`u32be(len("exomem.source-export-attestation/v1")) ||
"exomem.source-export-attestation/v1" || u64be(len(jcs_claim_bytes)) ||
jcs_claim_bytes` and emits the raw 64-byte signature in unpadded base64url; the
signature is not a claim field. The source private key never
leaves cell/machine custody and no shared source HMAC secret is given to the
destination. Destination trust is a private no-follow
`SourceExportVerifierRecord/v1` keyed by signer key id and binding exact public
key. The key is the raw 32-byte Ed25519 public key encoded unpadded base64url;
`key_id` is `ed25519-sha256:` plus lowercase SHA-256 hex of those raw bytes. The
record also binds purpose=`vault-consolidation-export`, audience/destination trust domain,
exact allowed source vault and installation/generation (plus typed Hosted cell
where applicable),
status, not-before, not-after, revocation time/reason, and registry generation.
Unknown, inactive, premature, expired-at-issue, revoked-at-issue-or-verification,
wrong-purpose/audience/source keys fail closed.

Verification occurs independently at intake, apply, retirement clearance, and
retirement consumption. Rotation permits an explicitly bounded overlap of two
public keys while each attestation remains bound to one key id and issue time;
removing/revoking a key does not become an excuse to trust it later. Public
verification records and revocation history needed to verify the retained
attestation remain in destination/private custodian custody through retirement
and the configured evidence-retention period. Fixed RFC 8032 plus Exomem
canonical-claim vectors pin signing and rejection across runtimes. A named
control-plane receipt alternative must bind the same claims and have an equally
closed issuer/purpose/audience/source/status/validity/revocation trust record;
mere transport authentication or a caller-supplied receipt does not qualify.

A Hosted proof additionally binds its typed routing `source_cell_id`; a local
proof omits that Hosted-only field. Cell, vault, and installation ids occupy
distinct namespaces, and an aliased cell/vault value is rejected as malformed.

The immutable source fingerprint is SHA-256 over the common JCS frame with ASCII
domain `exomem.consolidation-source-fingerprint/v1` and a closed object containing
the verified claims plus attestation/receipt digest. The archive is copied or referenced
by content address and never altered in place. Any changed byte, identity,
attestation, or expiry requires a new run.

The canonical source content census uses the same fixed structural exclusions
as the destination census for every `_Consolidation/**` subtree, receipt chain,
seal/journal/control state, runtime/derived state, and private staging. Archive
and manifest digests remain separate attested claims, so an excluded control or
evidence byte cannot silently enter the content/rollback preimage or escape
artifact-integrity verification.

Alternative: trust an expected archive hash typed by the caller. Rejected: it
pins bytes but does not prove which Exomem produced them. Alternative: query a
live source MCP. Rejected because source-side release filtering makes a complete
inventory impossible to prove.

### 3. Give local and Hosted cells durable non-caller-selected identities

Consolidation cannot assume a Hosted binding: standalone local cells use the
same identity contract. Each active cell has a private no-follow identity record
outside `Knowledge Base/` containing a logical `vault_id` and an
`installation_id`, authenticated by a cell- or machine-owned key that is not
exported with the vault. `vault_id` identifies the active logical corpus;
`installation_id` identifies the serving instance. Neither value, its trust key,
nor its root binding may be selected in a consolidation request.

Hosted `cell_id` is a routing/deployment identity, not the logical corpus
identity. `cell_id`, `vault_id`, and `installation_id` use distinct typed
namespaces and are recorded and compared independently; a record or assertion
that aliases `cell_id == vault_id` is malformed and fails before readiness,
export, owner-context construction, or consolidation admission. A Hosted cell
may be replaced while the logical vault persists only through the fenced
installation-transfer protocol below, never by treating its cell id as the
vault id.

A legacy unbound vault is adopted once through an authenticated local-owner
bootstrap under the exclusive lifetime/writer boundary. The engine inventories
the canonical root, generates both ids, atomically records an authenticated
binding to the stable canonical root/filesystem identity, stores the adoption
census as immutable provenance only, and registers installation ownership before
advertising it as consolidation-capable. Ordinary legitimate writes do not
rewrite or invalidate that identity record. An owner-authorized move/rebind of
that same installation verifies the prior private record and stable source root,
binds the new stable root/filesystem identity, and preserves both ids. A copied identity record, an installation
id already claimed by another root/cell, a missing machine trust key, or a
logical/root/fence collision fails closed; ordinary consolidation never adopts
caller-provided ids to repair it.

A rehearsal clone is created by an explicit clone operation, not by copying the
identity record. It receives a new active `vault_id` and new `installation_id`
and records immutable `clone_of_vault_id`, `clone_of_installation_id`, and
`clone_of_snapshot_digest` provenance. Thus two rehearsal clones are distinct
active cells while their proof still names the real logical lineages they model.
An offline failover restore that intentionally preserves a logical vault id
remains governed by the existing portability binding contract and is not a
rehearsal clone. A real consolidation requires distinct source and destination
logical vault ids and installation ids; a rehearsal requires distinct active
clone ids/installations and the approved distinct clone-of source/destination
lineages.

Local export authentication uses the cell/machine-owned signing key or a trusted
local control transport to issue the same detached attestation claims as Hosted.
The destination trusts only configured machine/cell public identity or a trusted
control channel; request-supplied keys and copied private identity files are not
trust roots.

Preserving a logical vault id across failover is a fenced identity transfer, not
a clone and not a copied binding. The private identity record also carries a
monotonic `installation_generation` and the digest of its active-installation
fence. The target generates its own installation id and challenge. An
out-of-band authenticated `vault-identity-transfer/v1` binds the logical vault,
source installation/generation, target installation/challenge, exact export and
census, source quiescence checkpoint, transfer operation, issue/expiry, and
target generation `N+1`.

Under the configured authoritative installation registry, transfer uses one
compare-and-swap to fence/deactivate source generation N and reserve target
generation N+1 before the restored target can become ready. Target activation
then consumes that reservation, and source readiness/export/mutation rejects
its stale fence. Crash recovery recognizes only exact source-active,
source-fenced/target-pending, or target-active states. Two active installations,
a skipped generation, a missing/unreachable fencing authority, or a target id
chosen by caller input fails closed. A disaster-recovery environment unable to
fence or revoke the old installation may prepare an offline candidate but may
not activate it under the same logical vault id; it must obtain authoritative
fencing or explicitly adopt a new logical lineage.

This extends rather than contradicts portability restore: the archive still
contains no live runtime binding, the target still receives fresh private state,
and an identity-aware restore preserves logical id only after the external
transfer proof. A legacy archive without the identity-transfer contract may use
the existing restore-candidate path, but it is not consolidation-capable until
owner adoption as a new lineage or a valid fenced transfer completes.

Current content is never inferred from the identity record's adoption census.
Export computes the current canonical census only after quiescence, signs it in
the source checkpoint/attestation, and apply and retirement revalidate that
current checkpoint. Therefore adopt -> ordinary legitimate write -> quiesce ->
export is valid and produces a new signed census without re-authenticating the
identity record, while a copied root/binding or stale installation fence still
fails.

### 4. Separate durable control state from private plaintext staging

The destination owns a canonical run at
`Knowledge Base/_Consolidation/runs/<run_id>/`. It contains lifecycle,
fingerprints, bounded inventories, conflicts, decisions, plan preimage,
journals, probe definitions/outcomes, and artifact digests. `_Consolidation` is
structurally reserved: generic file operations cannot mutate it, corpus walkers
cannot index it, and normal content projectors cannot release it. Only the
owner-authorized consolidation command can return its detailed state.

Source extraction, candidate bytes, and the destination rollback preimage live
in a private artifact/staging root outside both `Knowledge Base/` and every
recall walk. The run stores opaque artifact references and hashes, never an
absolute staging path. Hosted uses its private artifact store; local installs
use the same abstraction over a durable state root. Loss or mismatch of a
required artifact blocks apply/rollback and, in particular, blocks source
retirement.

This intentionally differs from Adoption Studio's canonical raw staging. A
consolidation archive already contains governed private bodies; placing its
extraction under any vault path that `scope="vault"` can walk would create a
pre-policy disclosure path.

### 5. Fingerprint the destination independently of run and receipt churn

The destination canonical census is a sorted manifest of normalized relative
path, entry type, byte size, and SHA-256 for every canonical owned file. It
includes knowledge, append-only artifacts, authored history, access metadata,
review state, and active policy source, while excluding rebuildable indexes,
runtime logs/locks/caches, every `_Consolidation/**` subtree, every
governance/consolidation/mutation/disclosure receipt chain, all seal/operation
journals and markers, and private staging. Policy, access, and review-state fingerprints are also
recorded separately so a caller can diagnose which guarded component drifted.

The immutable destination snapshot fingerprint is SHA-256 over the common JCS
frame with ASCII domain `exomem.consolidation-destination-snapshot/v1` and a
closed object containing logical destination vault id, destination installation
id/generation and active-fence digest, authenticated identity/root-binding
fingerprint, canonical census digest, policy fingerprint,
access fingerprint, and review-state fingerprint. Planning and approval bind
that value. Apply acquires the seal and repeats the identity binding and census
before consuming the approval; drift refuses rather than being folded into the
run.

Excluding all run/evidence/control churn is necessary: creating, approving,
recovering, or auditing any current/older/concurrent run must not stale or enter
the destination content snapshot, preimage, or rollback target. The active
run's immutable materialization `control_basis_digest` and declared semantic
predecessor are bound in the plan; each later journal binds its own current
predecessor/seal/journal digests. Neither requires the mutable reserved subtree
or global receipt head to remain equal to the materialization-time value. The exclusions are structural and
fixed by schema, not caller-selected, and no rollback ever restores or rewinds
any excluded control/evidence subtree.

### 6. Use an ordered C1-C8 reconciliation matrix

The planner builds exact byte, normalized-path, stable-identity, logical-content,
attachment, semantic-anchor, relation, history, record-item, and policy/control
indexes for both inventories. Each source object receives exactly one primary
class using this precedence; lower-level dependency findings remain attached to
the row:

- **C8 authority/control state:** source policy, audiences, grants, tokens,
  sessions, receipts, run state, runtime binding, access control, or review
  decisions that cannot become live destination authority by copying.
- **C6 divergent identity collision:** the same durable identity names different
  exact object bundles.
- **C5 divergent path collision:** the same normalized destination path names
  different exact bytes without a C6 identity collision.
- **C4 content-equivalent identity divergence:** deterministic logical content
  (removing only the governed identity field) is equal while durable identities
  differ or are missing.
- **C3 identity-equivalent relocation:** durable identity and exact object bytes
  match, but paths differ.
- **C1 exact duplicate:** normalized path and exact object bytes match.
- **C7 dependent structural conflict:** the object has no direct path/identity
  collision, but a semantic-unit anchor, link/ref, supersession/history edge,
  typed relation, media/sidecar pairing, Record identity, type/lifecycle rule,
  or append-only invariant becomes ambiguous or invalid under the tentative map.
- **C2 unique addition:** none of C1 or C3-C8 applies.

C1-C3 have deterministic no-loss defaults. C4-C8 are unresolved until the owner
reviews an allowed finite resolution. Append-only Sources and Evidence may be
kept, deduplicated only by exact bytes, or placed at a new collision-free path;
they are never overwritten or body-rewritten. Compiled pages may keep both,
choose one identity, relocate, or create an explicit supersession/reconciliation
only when the plan names the exact before/after hashes. C8 source authorization
artifacts are preserved in the source archive for provenance but never copied as
live authority. No ineligible object or unresolved dependency is silently
dropped.

A C1 no-op is only a publication no-op. Its owner-only reconciliation row
durably maps the authenticated source snapshot/object/path/identity/hash to the
exact destination object/path/identity/hash, contributes to the reconciliation
and plan digests, and survives completion or rollback according to run
retention. The content-free receipt binds the mapping-set digest. Exact-byte
deduplication therefore does not erase the fact that the destination object
represented both inventories.

Alternative: filename-first merge plus post-hoc broken-link repair. Rejected
because identity, history, and access conflicts would become visible only after
publication and could not be bound into the user's review.

### 7. Translate intent onto fresh destination principals

Source audience ids are installation/session facts, not portable identities.
The agent may propose roles and purposes in plain language, but every principal
used by the prospective destination policy and disclosure matrix must have a
fresh `destination-principal-attestation/v1` from a trusted destination surface.
The attestation binds the destination vault, issuer/surface, resolved canonical
principal, purposes, issue/expiry, authentication/session binding, nonce, and
attestation fingerprint. The destination resolves the principal; the request
body cannot select it.

Source rules are inputs to the owner's reconciliation decision, not executable
documents. The plan emits newly authored destination scope/rule/bridge documents
through the existing governance grammar and existing exact release-approval
primitives. It copies no source audience id, grant, token, authorization session,
or release approval. A missing, expired, copied, or destination-mismatched
principal attestation blocks approval and apply.

### 8. Bind one exact owner confirmation to content and policy

`plan` materializes canonical JSON `exomem.consolidation-plan/v1`. The exact
preimage has these required fields:

- schema and protocol versions, run id, and run mode (`cloned-rehearsal` or
  `real-cutover`);
- immutable source and destination snapshot fingerprints;
- expected destination-preimage census digest;
- sorted source inventory, reconciliation, conflict-decision, identity-map,
  path-map, and dependency-map digests;
- exact content actions, expected before hashes, planned after hashes, and
  journal batch partition digest;
- exact canonical policy documents, prospective policy fingerprint, bridge and
  exact-release approval fingerprints, plus the digest of the exact canonical
  destination-policy bundle from which those values were reviewed;
- fresh destination owner/principal attestation-set digest;
- representative principal x purpose x item disclosure-matrix digest;
- positive/negative verification-plan digest;
- nonterminal apply rollback-contingency digest and required source-retention
  state through cutover and its approved recovery window; this is not a future
  terminal-run rollback plan, which must be materialized from then-current state;
- immutable `control_basis_digest` and closed
  `plan_successor_automaton_digest`, never the current mutable run-state or
  current physical receipt-head digest;
- a server-derived impact summary covering every create, overwrite, removal,
  relocation, deduplication/provenance mapping, policy/principal/disclosure
  change, batch, rollback consequence, surviving-copy obligation, and unresolved
  count; and
- plan creation time, plan validity deadline, and a fresh plan nonce.

For a cutover plan, the owner-only plan directory also contains immutable
`policy-bundle.json`: the canonical bytes of
`exomem.consolidation-destination-policy/v1`. The bundle carries the exact
destination vault binding, prospective compile target and authoring snapshot,
document edits and document-set digest, source-authority review records/digest,
fresh principal attestations and attestation-set digest, principal requirements,
and named principals. Its framed-JCS digest is outside those bytes; the cutover
plan binds it as `policy_bundle_digest`. The bundle deliberately contains no
`plan_digest`, avoiding a digest cycle. `plan.json`, `control-basis.json`, and
`policy-bundle.json` are one immutable stored cutover-plan object: missing,
partial, changed, non-canonical, digest-mismatched, or legacy unbound state is a
closed refusal rather than authority reconstructed from caller fields.

At apply and every recovery continuation, the server loads those exact stored
bundle bytes, cross-checks their digest, destination binding, policy documents,
prospective policy fingerprint, principal-attestation-set digest, and plan
nonce against the stored plan, then reruns prospective compilation and fresh
destination-principal/session attestation validation. This check completes
before any governance policy mutation. A caller-supplied bundle, digest-only
claim, or freshly reconstructed approximation cannot authorize publication.

Every consolidation source-export claim, cutover, rollback, rendering,
retirement, event, and fingerprint object uses one canonical encoding: RFC 8785 JSON Canonicalization
Scheme over a closed JSON value subset. All input strings are valid Unicode and
normalized to NFC before schema validation; object duplicate keys (including
duplicates created by NFC normalization) are refused; JCS escaping and UTF-16
property ordering apply; timestamps are UTC RFC 3339 strings with fixed
millisecond precision; hashes/ids/enums/paths are strings; booleans are JSON
booleans; counts, ordinals, generations, byte sizes, and TTL milliseconds are
schema-bounded non-negative integers no greater than 2^53-1; no other numeric
values, negative zero, floats, exponent forms, NaN, infinity, or null are
admitted unless a field explicitly defines null. Paths are separately normalized
by the path contract before JCS.

Hash framing is `u32be(len(domain_ascii)) || domain_ascii ||
u64be(len(jcs_bytes)) || jcs_bytes`, where the closed ASCII domain string names
schema and version. Fixed cross-runtime vectors pin Unicode/escaping/order,
integer boundaries, framing, and digest bytes. `plan_digest` is SHA-256 over the
framed `exomem.consolidation-plan/v1` canonical bytes; the digest is not a field
inside the bytes it hashes. The full
preimage and computed digest remain in owner-only run state.

`control_basis_digest` is SHA-256 over framed
`exomem.consolidation-control-basis/v1` JCS. Its closed object binds run id,
plan kind, plan-materialization operation id, basis run revision, immutable
source/destination fingerprints, the complete immutable plan-input-set digest,
the fresh plan nonce, the committed same-run semantic predecessor event id/payload digest resolved
from the materialize successor context, and
`plan_successor_automaton_digest`. It does not include `plan_digest`, mutable
render/approval/token rows, the full reserved run subtree, or a receipt chain's
current head. It is an immutable ancestor recorded at materialization, not a
snapshot that later expected review events must equal.

The automaton object contains exactly `schema`, `initial_state`, `states`,
`terminal_state`, `minimum_pages`, `page_count_source`, `transitions`,
`retry_rule`, and `unexpected_event_rule`. Its values are fixed as follows:

- `schema=exomem.consolidation-plan-successor-automaton/v1`,
  `initial_state=plan-materialized`, `terminal_state=token-reservation`, and
  `minimum_pages=1`;
- `states` is the exact ordered array `plan-materialized`, `render-begin`,
  `render-page`, `render-ack`, `render-complete`, `approval`,
  `token-reservation`;
- `page_count_source=stored-plan-rendering-definition`;
- `transitions` is an ordered seven-row array whose objects contain exactly
  `ordinal`, `from_state`, `to_state`, and `guard`: `(0,
  plan-materialized,render-begin,once)`, `(1,render-begin,render-page,page-0)`,
  `(2,render-page,render-ack,same-page)`, `(3,render-ack,render-page,
  next-page-if-any)`, `(4,render-ack,render-complete,last-page)`, `(5,
  render-complete,approval,complete-coverage)`, and `(6,approval,
  token-reservation,matching-kind-token)`; and
- `retry_rule=adopt-existing-identical-event` and
  `unexpected_event_rule=stale-plan`.

`plan_successor_automaton_digest` is SHA-256 over the common framed JCS bytes
under exact ASCII domain
`exomem.consolidation-plan-successor-automaton/v1`; the digest is outside the
object. The object is deliberately protocol-static and contains no plan digest,
control-basis digest, run id, or page digest, avoiding recursive hashing. The
plan separately binds its server-derived ordered rendering definition and page
count, while the control basis binds this automaton digest to that plan's run,
kind, materialization operation, nonce, and immutable input set.

For a stored plan with `N >= 1`, the transition guards expand to
`plan-materialized -> render-begin -> render-page(0) -> render-ack(0) -> ... ->
render-page(N-1) -> render-ack(N-1) -> render-complete -> approval ->
token-reservation`. Every
successor's nested event payload names the exact preceding committed event id
and payload digest, same run, plan kind/digest, and its own operation id. An
identical retry adopts the same event and adds no transition. A skipped,
duplicated, reordered, wrong-plan/run, parallel rendering, reconcile, new-plan,
or other same-run control/evidence event is outside the automaton and makes the
plan stale. `status` creates no semantic-successor event, although owner-detail
may return the already-durable current context pair; unrelated-run events may interleave on
the physical receipt chain without changing this semantic ancestry.

At apply admission, the request resolves the successor context whose predecessor
is the matching `approval` terminal. After exact automaton and JTI validation,
the one allowed token reservation is committed and ends the review automaton.
The apply journal then binds that committed terminal's id/payload
digest as `apply_predecessor_event_id`/`apply_predecessor_digest` and revalidates
it in the separate `seal-intent` effect and every recovery continuation. No
additional review-automaton state or event exists between token reservation and
apply's `seal-intent`. Apply never requires the current global receipt
`prev`/head or the whole mutable run-control
tree to equal the old plan preimage. Terminal-plan rollback and retirement
clearance use the same kind-bound rule; nonterminal-contingency rollback instead
uses the original apply's exact current semantic predecessor and already
reserved contingency authority.

Approval proves trusted rendering of those stored bytes, not merely knowledge of
their digest. `plan(operation=render)` loads the plan by run id, plan kind, and
digest from reserved state; it never renders caller- or agent-supplied plan text.
The server fixes the ordered section list and page boundaries and returns each
page with its section id, ordinal, page digest, total pages/rows, plan digest,
and the server-derived impact summary. A trusted owner surface records explicit
page acknowledgements in one authenticated owner/session/surface binding. Only
after every required section/page digest has been served and acknowledged does
the server mint `plan-rendering-completeness/v1`, binding plan kind/digest,
ordered section/page digests, totals, impact-summary digest, owner principal,
issuer/surface, authorization session, issued/expiry, and nonce. This proves
complete trusted presentation and acknowledgement, not subjective comprehension.

Each durable acknowledgement requires surface-injected
`PlanRenderAcknowledgement/v1` binding owner/session/issuer, render session,
run, plan kind/digest, server section/page ordinal/digest, impact-summary digest,
issued time, and nonce. A request's expected page digest is only an optimistic
check; without this separate trusted context it records nothing. Thus an agent
that learns all digests still cannot manufacture page coverage.

`approve` re-loads the exact stored plan and requires that unexpired
rendering-completeness capability plus an exact expected plan digest. An
agent-supplied digest, copied display, untrusted preview, skipped/truncated page,
or `approved=true` request field is never evidence. CLI uses an authenticated
TTY pager/acknowledgement flow; Hosted uses its authenticated confirmation route;
MCP/REST use trusted host elicitation or the same out-of-band rendered bundle.
The agent cannot mint the capability. The resulting wire token contains only
version, plan kind, run id, plan digest, rendering-completeness digest, JTI,
expiry, and an authenticated signature/MAC.

The token is single-use and plan-kind-bound. Apply, rollback, or retirement
reserves its JTI for one matching operation journal; a retry may resume that
operation, but no second operation or plan kind can reuse it.

### 9. Seal every ordinary content surface, including the owner's

Apply first acquires the destination writer authority and transitions to a
durable destination-wide seal. The seal drains admitted reads, writes,
transfers, and background writers, then rejects all new ordinary content reads
and mutations. The owner is not exempt: normal owner `ask`, `read`, `browse`,
media, review, graph, history, export, and file operations receive the same
content-free sealed response as every other principal.

The only exception is an unforgeable, in-process `ConsolidationAuthority` bound
to the destination vault, run id, operation journal, exact phase, and allowed
action. It is never serializable and never accepted as a command argument. The
trusted owner control path can use it to read the reserved run, publish the exact
plan, restore the preimage, and invoke named verification probes in-process. A
pre-unseal probe bypasses only the outer seal and calls the same
adapter/serializer pipeline internally with an ordinary freshly attested
representative principal; it still traverses identity resolution, release
decision, projector, scrubber, response adapter, and receipt collector. The
capability cannot cross an MCP, REST, CLI, or Hosted network/process boundary
and is never injected into a black-box request. Transport-level proof uses the
exact destination later under stopped public routing, normal authentication,
and no consolidation authority.

The seal marker and phase load before command admission after restart. Missing,
conflicted, malformed, or journal-inconsistent seal state fails closed. The
public sealed response does not reveal run id, phase, counts, policy state,
whether an item exists, or which recovery branch is active.

Alternative: rely on restrictive policy alone. Rejected because policy has no
audience-independent quarantine for partially published bytes and because even
an owner could observe an inconsistent graph/index mid-saga. Alternative: seal
only writes. Rejected because readers could observe policy/content publication
between batches.

Hosted represents lifecycle sealing as a typed effective union, never one
overloaded boolean: `open`, irreversible `deletion-sealed(checkpoint)`, or
`consolidation-sealed(vault_id, run_id, operation_id, phase, journal_digest)`.
Deletion seal dominates the effective union and can never be reopened by any
generic `resume`, consolidation recovery, or consolidation unseal. A
consolidation seal can be advanced or removed only by the exact bound journal;
foreign/stale run or operation ids fail closed. Existing export/quiescence states
compose explicitly and never erase either seal kind.

All consolidation actions remain semantically write-capable except `status`,
but lifecycle-changing actions do not run inside Hosted's ordinary full-leaf
`admit_mutation()` wrapper. `apply`, an apply resume, `recover`, `abort`,
`rollback`, and `verify` use an owner-control mutation admission lane. It
authenticates `ConsolidationOwnerContext/v1` before body/run lookup, atomically
reserves operation id/JTI/idempotency and exclusive writer/lifecycle authority,
then converts exactly its own admission from the ordinary active-mutation
counter into a journal-bound control-operation participant before drain. The
conversion is compare-and-swap and crash-recoverable: exactly that operation is
excluded; no other mutation/read/transfer/background participant is. Phase-bound
internal batches subsequently execute under the seal and same exclusive
authority. A lifecycle-changing consolidation leaf routed through ordinary
full-leaf mutation admission is a startup/coverage failure because it would
self-deadlock waiting for its own active count.

After restart, generic Hosted dispatch remains sealed, but an explicit private
owner-control lane admits only `status`, `recover`, `abort`, `rollback`,
`verify`, and a necessary identical `apply` resume. It bypasses only ordinary
lifecycle/outer-seal admission, authenticates owner and matches the exact
vault/run/operation/journal before detailed lookup, then constructs the
phase/action-bound in-process authority. It cannot start a new apply or cross a
deletion seal. Missing/mixed journal state remains reachable for bounded owner
status/recovery without reopening public or ordinary command admission.

### 10. Apply policy first through a journaled saga

The state machine is:

`approved -> sealing -> sealed -> preimage-ready -> policy-active -> publishing
-> rebuilding -> verifying -> verified -> transport-stopping ->
transport-verifying -> transport-verified -> routing-opening -> complete`.

The exact order is:

1. Resolve the request's owner-only successor context, validate its protected
   approval predecessor against the immutable control basis and successor
   automaton, reserve the approval JTI and operation id, persist the committed
   token-reservation event as the apply journal's
   separate current predecessor, acquire exclusive writer/lifecycle authority,
   revalidate that predecessor, then seal and drain the destination. Revalidate
   the source artifact, both snapshots, the exact stored destination-policy
   bundle and its fresh principal attestations, conflict decisions, and complete
   plan preimage without comparing the current global receipt head/full mutable
   run control tree to their materialization-time values. Missing, changed, or
   unbound policy-bundle bytes refuse before governance mutation.
2. Materialize a full content-addressed destination preimage in private artifact
   storage. Verify every entry and bind its manifest digest to the approved
   destination census before continuing.
3. Activate only the exact stored and revalidated restrictive destination-policy
   bundle through the existing governance journal/marker/critical-receipt
   protocol. The seal remains the outer floor.
4. Publish exact content actions in bounded deterministic batches through
   `batch_atomic_write`. Each batch has prior/prepared/final fingerprints,
   receipt-first intent, a durable run-journal transition, and idempotent exact
   current-state classification. The first committed content batch is the
   publication boundary.
5. Rebuild lexical, embedding, semantic-unit, graph, media, freshness, identity,
   and review derivatives only from canonical destination bytes. Derived state
   is never copied from the source archive.
6. Run the approved positive and negative verification matrix in-process through
   the same adapter/serializer functions under the narrow probe capability and
   ordinary representative authorization contexts. Any failure keeps the
   destination sealed.
7. Persist and verify a trusted control-plane proof that public ingress/routing
   for the exact destination is stopped and all prior public transport work is
   drained. Bind the post-cutover census, running release/build digest, selected
   surface profile/descriptor, configuration/trust/principal-mapping
   fingerprints, and routing proof into the transport-verification plan.
8. Under control-plane supervision enter `transport-verifying`: remove or bypass
   only this operation's consolidation seal as needed by normal adapters while
   public routing remains durably stopped. A supervisor-owned isolated listener
   or equivalent OS/control-plane route admits only the precommitted probe
   connections and is not represented in request/authentication data; arbitrary
   public/local clients remain blocked. Exercise real MCP, REST, Hosted, and
   CLI paths on the exact destination using normal authenticated principals and
   no serialized `ConsolidationAuthority` or special principal shortcut. Persist
   the bound positive/negative outcomes and recheck every bound fingerprint.
9. On success append the transport-verified terminal, then atomically authorize
   public routing and complete. A failure or restart never opens routing: it
   deterministically reinstates the same consolidation seal or remains in
   owner-only recovery, from which the approved rollback remains available.

Clone transport evidence is rehearsal evidence only; it cannot substitute for
the exact-cell `transport-verifying` gate of real cutover.

Policy-first remains mandatory even though the destination is sealed: it makes
the safe ordering explicit and prevents an unseal/recovery bug from exposing
imported bytes under an older policy.

### 11. Recover by exact state classification, never semantic replay

Every phase and batch has domain-separated prior/prepared/final fingerprints.
`status`, startup, and `recover` compare the current seal, policy, canonical
files, artifact manifests, derived readiness, and receipt terminals with those
fingerprints. Exact prior can abort; exact prepared can finish missing evidence
and activate; exact final can advance; a mixed or third state remains sealed and
reports owner-only repair facts. Recovery never asks the agent to re-derive a
decision and never invokes a content mutation twice.

Changed retry arguments, artifact refs, attestations, plan/token, destination
identity, or run mode conflict with the journal rather than adopting it.
Acknowledgement loss returns the existing mutation reconciliation terminals;
callers retain the same mutation identity and payload.

Receipt JSONL/anchor state, SQLite idempotency/registry state, reserved run
state, the operation journal, the private artifact store, the governance
journal, and canonical filesystem state are separate failure domains;
the design claims no transaction across them. Every durable effect, including
start, intake, each snapshot, reconcile, each plan/render page/acknowledgement,
approval/token reservation, seal/drain/preimage/policy, every publication batch,
each rebuild and probe, transport stop/probe/terminal/routing open, abort,
rollback, recovery, and retirement plan/clearance/consume/completion/finalize,
plus forward-snapshot verification, each surviving-copy-ledger calculation,
pending-forward-only fence installation, expiry/non-consumption fence release,
and the retirement-finalize target that includes permanent fence conversion,
uses the existing outer `receipt/v1` envelope unchanged. Its `schema`,
`event_id`, `event_type=consolidation`, `phase`, `timestamp`, `instance_id`,
`seq`, physical `prev`, `durable`, and record `hash` remain envelope fields. The
only consolidation payload member is a nested closed
`consolidation_event` object with schema
`exomem.consolidation-event/<kind>/v1`; the nested schema never replaces or
masquerades as the outer receipt schema.

The nested common fields are `schema`, `kind`, `run_id`, `operation_id`,
`phase`, `record_role`, `effect_ordinal`, the applicable
batch/rebuild/probe/page ordinal, canonical `request_digest`, exact
`prior_digest`/`target_digest`, `prepared_digest` exactly when the kind defines a
distinct prepared state, required closed digest-only `evidence` plus its
`evidence_digest`, `semantic_parent_event_id`,
`semantic_parent_payload_digest`, and `payload_digest`; only terminal roles add
required `observed_digest`. `payload_digest` is SHA-256 over the common frame
with ASCII domain
`exomem.consolidation-event-payload/<kind>/<record_role>/v1` and the nested
closed JCS object excluding `payload_digest`. Intent forbids
`observed_digest`; committed/aborted requires it. Nested `record_role` equals
outer `phase`, and every consolidation record is durable.

Every kind's `evidence` is an exact closed object with schema
`exomem.consolidation-event-evidence/<kind>/v1`, matching kind, and only the
named 64-hex digest fields applicable to disposition, checkpoint, JTI,
render/coverage/impact, verification basis, forward snapshot, surviving-copy
ledger, or permanent fence. It contains no proof body, path, ref, principal,
credential, token, timestamp, count, enum, or caller extension. Its
`evidence_digest` is SHA-256 over the common frame with that schema's ASCII
domain and the exact evidence bytes. Intent construction, append, verification,
and recovery fail closed unless the kind-specific object is exact and its
digest matches; underlying proof bodies stay owner-protected.

For an intent whose committed target would produce a successor context, the
intent and its matching committed or aborted terminal additionally require
`successor_context_seed_digest`; every other nested payload forbids it. The
intent target and committed observed-state preimages include that same closed
seed digest and never include the full context digest/ref or terminal id/payload
digest. Thus the terminal payload digest commits only the predecessor-free seed.

The outer intent `event_id` remains exactly 64 lowercase hex: SHA-256 under
`exomem.consolidation-event-id/<kind>/v1` over the intent identity fields above,
including its declared semantic parent, but excluding `payload_digest`. The
outer terminal id preserves the existing receipt contract and is exactly
`<intent-event-id>:committed` or `<intent-event-id>:aborted`; it is not a second
64-hex event id. Its distinct role-specific `payload_digest` carries terminal
identity. Fixed vectors pin outer ids and nested digests separately.

Outer `prev` always equals the actual current local receipt-record hash at append
time and may therefore name an unrelated interleaved event. It is physical chain
ordering, never semantic causation and never part of the deterministic intent
id. `semantic_parent_event_id`/digest follows the causation table: only `start`
uses the fixed root equal to SHA-256 of framed empty JCS under
`exomem.consolidation-semantic-root/v1`; every other intent names its declared
prior semantic terminal, and each terminal names its matching intent. Thus a
different run can change physical `prev` without staling a plan, while a wrong
same-run semantic parent is refused.

The cross-store order is fixed. A successor producer first fsyncs its inert
seed/ref subrecord inside the existing idempotency reservation; that subrecord
changes no semantic state/revision and is not resolvable. Then: (1) append/fsync
the consolidation intent binding the seed through its target and observe outer
sequence/hash/current physical head; (2) persist/fsync journal `prepared`
referencing the intent outer id, nested payload digest, outer record hash/
sequence, seed when applicable, and exact request/prior/prepared/target digests;
(3) perform exactly one effect; (4) classify current state as prior, prepared,
target, or mixed/third; (5) append/fsync exactly one committed or aborted
terminal with the compatible suffix id, matching semantic parent, observed
state, and same conditional seed; and (6) persist/fsync journal `final`
referencing its outer id, nested payload digest, record hash/sequence, and new
physical head. A committed successor producer's step 6 also derives and stores
the full context/ref/digest from the seed plus terminal; no event/revision is
added. Only afterward may the action-level idempotency record fsync canonical
logical terminal `T` and a response be returned.

Recovery tests interrupt every gap, including seed-reservation-before-intent,
terminal-before-context-final, and final-before-idempotency-terminal. Intent
without prepared state can close aborted only on exact prior; prepared plus
prior may resume only that authorized effect; exact target repairs the missing
terminal; terminal without final deterministically reconstructs only final and
its full context; final without idempotency terminal writes only byte-identical
`T`. A journal, seed, context, or receipt mismatch and any mixed/third state
remain sealed/blocked. No store is trusted to prove another committed.

### 12. Materialize every explicit rollback as an exact reviewed plan

Recovery of a still-nonterminal apply may execute only the rollback contingency
already bound by the approved cutover plan. Any owner-requested rollback after
the publication operation has reached a terminal uses
`plan(plan_kind=rollback, operation=materialize)` and canonical schema
`exomem.consolidation-rollback-plan/v1`; `rollback` itself is only the executor.
There is no hidden repair endpoint.

The tagged rollback request is a closed `oneOf` selected by `rollback_mode`;
both branches require the current opaque successor-context ref/digest.
`nonterminal-contingency` resolves it server-side to the original apply
operation id/journal digest, cutover-plan and approved contingency digests,
current publication-state digest and run revision, owner-only contingency-
authority ref/digest, exact current same-run control predecessor id/digest, and
deadline. Those protected fields are forbidden in the body. It is admissible
only while that apply is nonterminal and sealed,
inside its approved recovery window, and before any terminal apply outcome. It
forbids rollback-plan/token fields and consumes no new human approval; the
purpose-bound durable authority can be used only once for the matching original
apply/contingency, with identical retries returning its committed logical
terminal. `terminal-plan` requires the separately materialized/rendered/approved
rollback plan and rollback token and resolves its approval predecessor through
the matching successor context; it
forbids every original-apply contingency field. Either branch conflicts on a
changed mode, revision, context/predecessor, authority, journal, plan, or payload and
uses the same explicit rollback state machine and evidence without an implicit
target.

The rollback preimage contains:

- schema/protocol version, plan kind, run id, plan-materialization operation id, target
  (`pre-cutover` or a named `post-cutover-forward` snapshot), creation time,
  deadline, and nonce;
- original cutover plan/terminal digests; source and destination logical vault,
  installation, generation, identity-binding, and snapshot fingerprints;
- current canonical census/policy/access/review fingerprints and the recorded
  pre-cutover, post-cutover, and target snapshot/manifest/census digests;
- current source/archive retention proof, retirement/finalization state,
  post-retirement rollback disposition, and any forward-snapshot artifact,
  retention-domain, and verification digests;
- the sorted union-inventory digest covering every current object, every target
  object, every imported object/C1 mapping, and every post-cutover create,
  modify, move, or delete, with exact current/target/origin fingerprints;
- for every union row, one explicit finite treatment: restore target, retain
  current at an exact collision-free path, reapply current after target,
  retain both at exact paths, or owner-confirmed discard; plus every dependency,
  conflict, surviving-copy proof, before hash, and planned after hash;
- exact target policy/access/review documents and fingerprints, deterministic
  journal batches, derived rebuild plan, positive/negative verification plan,
  receipt plan, rollback-of-rollback recovery artifact, and server-derived
  impact summary; and
- the trusted rendering schema/ordered-section definition used before approval.

No imported object is omitted because its target treatment is removal, and no
post-cutover write is silently overwritten because it appears unrelated. A
pre-cutover target may remove an imported active object only while a verified
source vault/archive copy will survive the operation and retirement remains in
`pre-cutover-reversible` mode. After forward-only retirement finalizes, the
planner permanently refuses a pre-cutover target. A forward target must contain
every imported byte and durable provenance mapping. Unresolved rows,
missing copy proofs, and implicit defaults block materialization.

The rollback digest is SHA-256 over the common frame with exact ASCII domain
`exomem.consolidation-rollback-plan/v1` and remains outside the canonical bytes.
The normal trusted render-all-pages completeness protocol and
`approve(plan_kind=rollback)` issue a single-use rollback-kind JTI. Execution
rechecks the exact current census and copy/retirement proofs before reserving the
JTI, then follows `rollback-approved -> rollback-sealing ->
rollback-revalidating -> rollback-restoring -> rollback-rebuilding ->
rollback-verifying -> rollback-complete`. Every phase is receipt-first with
prior/prepared/final fingerprints. Identical operation/payload retry resumes;
changed plan, token, target, census, treatment, retention proof, or identity
conflicts. A drift after rendering makes the plan stale rather than being folded
into it.

### 13. Keep abort, rollback, and retirement semantically distinct

**Abort** is available only before the first content batch commits. It removes
private staging, restores any policy change from the exact destination preimage,
closes the token/journal with aborted evidence, and unseals only after proving
the destination census equals the approved prior snapshot. The source remains
unchanged.

**Rollback** is required after the publication boundary. Nonterminal recovery
may invoke only the contingency already approved in the cutover operation;
every explicit terminal-run rollback consumes the exact separately rendered and
approved rollback plan above. It acquires/retains the seal and restores the
named complete verified target snapshot with its policy, knowledge, append-only
artifacts, history, access/review state, and canonical metadata, then rebuilds
derivatives and verifies the target census before unsealing. If the current
census differs from the recorded post-cutover census, `plan_kind=rollback`
inventories the drift; invoking `rollback` without that exact approved plan
returns `ROLLBACK_RECONCILIATION_REQUIRED`. It never overwrites later work
silently. Rollback never rewinds or deletes append-only governance,
consolidation, or mutation receipt evidence: receipt churn is outside the target
canonical census, and rollback appends its own intent, phases, and terminal so
the attempted cutover remains auditable.

**Source retirement** is neither abort nor rollback and is never implicit in
apply. `retire-source` can issue a content-free clearance only after a successful
cloned rehearsal (including a proven rollback), a separately approved real
cutover, current destination verification, an available verified preimage, an
unchanged authenticated source snapshot/quiescence checkpoint, and a fresh
owner confirmation bound specifically to retirement. The retirement preimage
also binds an exact declared source-archive disposition: retained under an
opaque verified artifact reference and retention term, transferred to another
named trusted custodian by an authenticated receipt, or authorized for external destruction after
clearance. Its canonical fields include retirement schema/version and
nonce/deadline; run and real-cutover plan/terminal digests; source/destination
vault, installation, snapshot, and current-census fingerprints; rehearsal and
rollback proof digests; destination verification and rollback-preimage artifact
digests/readiness; unchanged source checkpoint; exact archive disposition and
terms; and the retained-versus-irrecoverable provenance statement digest. The
schema and exact ASCII digest domain are
`exomem.consolidation-retirement-plan/v1`; the retirement digest is SHA-256 over
the common frame outside those canonical bytes and is what the third human
confirmation binds. The owner-visible preview states what remains
if the external archive is later destroyed: destination canonical bytes,
durable owner-only
source-to-destination reconciliation/C1 mapping and
identity/snapshot/attestation digests, and plaintext-free receipt evidence
remain; source-only bytes, source control state, and any provenance that existed
only inside the archive are no longer reconstructable. Actual routing stop,
retention, backup/key destruction, billing/account changes, or filesystem
deletion remain the authorized source/control-plane operator's responsibility.

A transfer disposition requires detached
`archive-custody-receipt/v1`, delivered by opaque ref and authenticated with the
same framed-JCS Ed25519 conventions. Its closed claims bind custodian identity
and retention domain digests, source vault/installation/generation digests,
exact archive, manifest, and source-census digests, transfer operation id,
retention-terms digest, accepted/issued/not-before/expiry times, nonce, and
signer key id. The exact field names are those enumerated in the portability
delta; `not_before <= accepted_at <= issued_at < expires_at`. Signed
bytes use domain `exomem.archive-custody-receipt/v1`; the raw 64-byte signature
is unpadded base64url and outside the claims.

Private `ArchiveCustodianVerifierRecord/v1` is the closed field set enumerated
in the portability delta: raw 32-byte unpadded-base64url Ed25519 key and derived
`ed25519-sha256:` id, fixed algorithm/purpose
`vault-consolidation-archive-custody`, allowed custodian identity/retention
domain and source lineage, destination trust audience, status/ordered validity,
conditional revocation time/reason, and bounded registry generation. Bounded
two-key rotation overlap and fixed valid/revoked/wrong-domain vectors apply.
Retirement plan/clearance and source consumption, plus every rollback plan and
rollback commit that counts the custodian copy as a survivor, independently
revalidate exact receipt claims, artifact availability/digests, current terms,
validity, and verifier status. A stale/revoked/mismatched receipt contributes no
surviving copy and blocks before approval or effect.

Retirement additionally binds one post-retirement rollback mode and enforces a
surviving-copy invariant over every imported object/provenance bundle:

- `pre-cutover-reversible` requires the authenticated source vault or archive,
  public verification trust history, and retention proof to survive for the
  declared rollback window. A rollback plan may target pre-cutover only while
  revalidation proves those source copies will remain after rollback.
- `forward-only` is mandatory before source/archive bytes may become
  irrecoverable. Before clearance, the destination creates a separately retained,
  content-addressed `post-cutover-forward-snapshot/v1`, verified against the
  post-cutover census and containing every destination byte plus every imported
  object, C1 mapping, identity/relation/history/citation/review/provenance record,
  policy/access state, and the public-key/revocation evidence needed to verify
  lineage. Finalization permanently records that pre-cutover rollback is
  prohibited; future rollback/recovery may target only this forward snapshot or
  a newly reviewed state that preserves every imported bundle.

Forward-only clearance issuance itself first persists a destination
`retirement-pending-forward-only/v1` rollback fence bound to the lifecycle ref,
forward snapshot, ledger, destination census, source/archive proof, and deadline.
From that point, and before the clearance can be consumed externally, all
pre-cutover rollback planning/execution is refused; only a forward-preserving
target is possible. If consumption/completion acknowledgement is lost, this
fence survives restart. `recover` may close an expired unconsumed clearance and
remove the pending fence only after proving from source-lifecycle evidence that
the JTI was never consumed and the unchanged source/archive still exists; any
uncertainty keeps the fence. Successful finalization converts it to the permanent
forward-only prohibition rather than creating the prohibition after destruction.

At every clearance, consumption, external completion, rollback planning, and
rollback commit, the engine computes a surviving-copy ledger. For each imported
bundle at least one verified surviving canonical source, authenticated archive,
destination object, or retained forward snapshot must remain after the proposed
effect. If any count would become zero, the operation fails before its first
destructive effect. A pre-cutover preimage alone never satisfies the ledger
because it predates imported bytes.

The ledger schema/domain is
`exomem.consolidation-surviving-copy-ledger/v1`. Its closed JCS object binds run,
governing retirement/rollback plan, proposed effect, current source/destination/
archive/forward-snapshot census and proof digests, and sorted rows of imported
`bundle_digest`, candidate survivor kind/proof digest, disposition, and bounded
post-effect survivor count. Its SHA-256 framed digest is outside the bytes;
owner-only rows may identify hashes, while receipts retain only the ledger digest
and bounded counts. A missing row, unverified candidate, or zero count invalidates
the ledger.

Retirement clearance is a single-use purpose-bound source-lifecycle capability,
not a reusable report. It contains retirement digest, destination no-loss and
verification proof digests, source checkpoint/census, archive disposition and
artifact digest, rollback mode/forward-snapshot proof, operation id, JTI,
deadline, and authenticated source-lifecycle audience. The source operator must
consume it under source lifetime/fencing authority before any routing, storage,
archive, key, or backup destructive step. Consumption revalidates expiry,
unchanged source checkpoint/census, exact disposition/artifact, current
destination verification/recovery/no-loss proof, operation id/JTI, and fence;
drift, expiry, replay, or mismatch consumes nothing and authorizes nothing.
For forward-only, it additionally verifies the destination pending rollback
fence and exact forward snapshot/ledger; a clearance lacking that already-
durable fence is not consumable.
Clearance issuance evidence is distinct from consume evidence, and an external
completion `source-retirement-completion/v1` attestation is verified separately
before `retire-source(phase=finalize)` converts the already-active pending fence
into the permanent forward-only prohibition or records retained custody. It
binds the retirement-lifecycle ref and clearance
JTI/digest, source vault/installation/generation and consumed fence, exact
disposition/artifact digest, source-operator completion operation id,
content-free outcome/time, source consume event id/digest and verified source
receipt-head digest, issuer/audience, and authentication-proof digest; it
is accepted only from configured source-lifecycle/control trust and cannot be
caller-authored. Exomem does not perform the external deletion.

The source consume intent/terminal lives on the source per-machine chain and
names the destination clearance event as external causation. The authenticated
completion carries the source terminal/head proof; the destination completion
event uses that source terminal as semantic parent while retaining its own local
`prev` chain. No cross-machine atomic commit is claimed.

### 14. Rehearsal proof cannot authorize cutover

A cloned rehearsal uses distinct active clone vault/installation bindings,
immutable clone-of lineage, and `run_mode=cloned-rehearsal`.
It must execute apply, positive/negative probes, crash/retry checkpoints, and a
full rollback. Its proof can satisfy a prerequisite of a later cutover plan, but
its snapshots, attestations, plan digest, token, JTI, and authority are not
reusable.

A real cutover requires fresh source/destination fingerprints, fresh destination
principal attestations, a freshly generated joint plan, and a separate owner
confirmation. Source retirement requires a third confirmation after cutover.
Shipping and verifying the capability does not perform any of these operational
runs and does not justify calling a real consolidation complete.

## Risks / Trade-offs

- [The full destination preimage can be large.] -> Make storage admission and
  free-space checks part of approval/apply, use content addressing and safe
  local copy/reflink optimizations where available, but never weaken the verified
  complete-preimage requirement before retirement.
- [A destination-wide seal temporarily removes even owner recall.] -> Keep batch
  size and rebuild/probe bounds explicit, expose owner-only progress through the
  control command, and prefer a bounded maintenance window. Partial-state
  invisibility is worth the availability cost.
- [A crash can outlive the client session that started it.] -> Persist seal and
  journal before publication, reconstruct authority only from exact owner/run
  state, and make startup fail closed until recovery classifies the state.
- [C1-C8 decisions can create a very large preview.] -> Persist the complete
  exact plan owner-only and return bounded pages plus stable digests/counts. The
  approval binds the whole plan, never only the displayed page.
- [Policy translation could accidentally weaken source confidentiality.] ->
  Treat source policy as review input only, require fresh destination
  attestations, prospective-compile the exact documents, and make every
  representative negative probe mandatory before unseal.
- [Negative probes can themselves leak through diagnostics.] -> Store only
  content-free outcomes in receipts and apply the same projector/scrubber to
  probe results; full expected/actual details remain owner-only run state.
- [A later write makes rollback operationally harder.] -> Refuse automatic
  rollback on any post-cutover census drift and require a newly reviewed
  reconciliation instead of overwriting the work.
- [Private artifact storage can be lost independently of the vault.] -> Make its
  hash/readiness explicit in status, block rollback/retirement when unavailable,
  and retain the source until the retirement gate proves all recovery artifacts.
- [Direct filesystem or external-model access bypasses the product boundary.] ->
  State this limitation in bootstrap, run results, verification claims, and
  operational handoff. The seal and release plane govern Exomem-mediated output,
  not arbitrary OS or human actions.

## Migration Plan

1. Merge and verify `harden-governance-for-consolidation`; rebase these deltas on
   its canonical governance/release contracts before product implementation.
2. Add pure snapshot, attestation, C1-C8, plan-preimage, token, and state-machine
   logic behind failing unit/property tests.
3. Add reserved run/artifact stores, seal admission, exact preimage, journaled
   batches, recovery, rollback, and receipts behind crash-seam tests.
4. Add `consolidate_memory` to the command registry and regenerate MCP, REST,
   OpenAPI, CLI, scaffold/bootstrap, and connector contracts. Add the exact
   v4-plus-command `hosted-alpha-agent-v5` descriptor and its own generated
   plugin/manifest, compatibility, selection, and promotion artifacts without
   changing or auto-promoting v1 through v4.
5. Run installed-wheel local and Hosted E2E, an adversarial security review, and
   the full release gates. Ship the capability without touching real vaults.
6. In a later operation, make verified clones and run the full rehearsal plus
   rollback. Generate a fresh real plan only after reviewing that evidence.
7. Apply the real cutover under its own confirmation. Keep the source intact and
   recoverable until a separate retirement confirmation clears source-side work.

Rollback of the software release disables new starts but must retain the old
binary or a compatible recovery command while any run is sealed or nonterminal.
A deploy must refuse to remove a schema/version required by an active run.

## Open Questions

None blocking. Detached source proofs use the fixed Ed25519/JCS/framing contract
and `SourceExportVerifierRecord/v1`. A named authenticated control-plane receipt
is an alternative only when its configured issuer record implements the same
closed claims, purpose, audience/source scope, validity, status, revocation, and
retention decision. Key provisioning and rotation remain deployment operations;
algorithm choice and caller-supplied trust are not request-time policy.
