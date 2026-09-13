## ADDED Requirements

### Requirement: Consolidation is one registered multiplexed product command

The product registry SHALL define one `consolidate_memory` command and SHALL
generate its MCP tool, `/api/consolidate_memory` REST route, OpenAPI operation,
CLI `consolidate-memory` subcommand, and generated capability documentation
from that single entry. The required `action` selector
SHALL contain exactly `start`, `status`, `reconcile`, `plan`, `approve`, `apply`,
`verify`, `recover`, `abort`, `rollback`, and `retire-source`. Surface adapters
SHALL dispatch to the same engine and return the shared success/error envelope
with the same stable codes and content-free public diagnostics.

The command SHALL accept bounded structured fields and opaque artifact,
attestation, run, and token references. It SHALL NOT accept an inline portable
archive, private staging path, source credential, caller-supplied verifier key,
serialized consolidation capability, or arbitrary executable resolution.
Unknown actions and arguments from a different action's schema SHALL be refused
rather than ignored.

Hosted SHALL expose the product command only through a new additive
`hosted-alpha-agent-v5` profile whose ordered command surface is exactly the v4
profile plus `consolidate_memory`. V1 through v4 profile membership, descriptors,
hashes, generated plugins/manifests, locks, clients, and registered evidence
SHALL remain byte-identical. V5 SHALL have its own versioned descriptor and hash,
generated plugin/manifest fixtures, compatibility/promotion evidence, and
explicit deployment selection; defining v5 SHALL NOT auto-promote an active
deployment. A cell not explicitly configured for v5 SHALL not advertise or
admit consolidation. Private artifact and trusted-confirmation routes SHALL
remain operator/control-plane seams beneath the v5 product command and SHALL
not become public command parameters.

V5 selection SHALL require a closed signed private
`HostedProfileSelection/v1` control-plane record binding typed cell/vault,
installation id/generation/active fence, profile id
and descriptor hash, release/protocol, Records reader version, identity schema,
consolidation run/seal/receipt reader versions, artifact-store readiness,
source-export/control-receipt verifier readiness, archive-custodian verifier
readiness, owner-confirmation readiness,
owner-entitlement-verifier readiness, exact-cell transport-supervisor readiness,
rollback/recovery closure, operation id, validity, signer, and record digest. Startup
SHALL validate the whole tuple against the running image/dependencies before
advertise/admit. Missing, partial, stale, unknown, incompatible, inferred, or
caller-made selection SHALL not choose v5 and SHALL leave an explicitly selected
v1 through v4 surface byte-identical.

Its exact fields SHALL be `schema`, `cell_id`, `vault_id`, `installation_id`,
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
`record_digest` SHALL be SHA-256 over the common framed JCS object excluding the
digest/signature fields with ASCII domain
`exomem.hosted-profile-selection/v1`. `signature_algorithm` SHALL equal
`Ed25519`; `signer_key_id` SHALL be `ed25519-sha256:` plus lowercase SHA-256 hex
of the raw 32-byte public key. Signed bytes SHALL be
`u32be(len("exomem.hosted-profile-selection-signature/v1")) ||
"exomem.hosted-profile-selection-signature/v1" || u64be(32) ||
raw_32_byte_record_digest`; `signature` SHALL be the raw 64-byte signature in
unpadded base64url.

Configured private `HostedProfileSelectionVerifierRecord/v1` SHALL contain
exactly `schema`, `algorithm`, `key_id`, `public_key`, `purpose`,
`issuer_id_digest`, `deployment_audience_digest`, `profile_id`, `status`,
`not_before`, `not_after`, `registry_generation`, and conditionally
`revoked_at` plus `revocation_reason_digest` exactly when `status=revoked`.
`algorithm=Ed25519`; `public_key` is the raw 32-byte key in unpadded base64url;
`key_id` is its derived `ed25519-sha256:` id; `purpose` is
`hosted-profile-selection`; `profile_id` is `hosted-alpha-agent-v5`; `status`
is `active|inactive|revoked`; registry generation is an integer in
`0..2^53-1`; and `not_before < not_after` SHALL hold. The issuer and deployment
audience digests SHALL bind the configured control plane and cell deployment.
Startup SHALL verify validity
at issue and startup time; rotation SHALL permit only an explicit bounded
two-key overlap. Unknown/caller-supplied, premature, expired, revoked,
wrong-purpose/issuer/audience/profile keys SHALL fail. Fixed signing, rotation,
and revocation vectors SHALL be generated across supported runtimes. Unknown
fields, digest/signature mismatch, or non-v5 profile SHALL fail closed.

#### Scenario: One registry entry generates every selected surface

- **WHEN** the command registry and selected v5 artifacts are built
- **THEN** MCP, REST, OpenAPI, CLI, v5 Hosted, and capability documentation expose the same eleven actions and parameter semantics
- **AND** no surface-specific consolidation action list or implementation exists

#### Scenario: Existing Hosted profiles remain closed

- **WHEN** v5 is added and a cell is configured for any profile from v1 through v4
- **THEN** its command membership, descriptor/hash, generated plugin/manifest, client contract, and registered evidence remain byte-identical and omit `consolidate_memory`
- **AND** only explicit selection of a compatible v5 deployment makes the product command available

#### Scenario: V5 selection readiness tuple is incomplete

- **WHEN** the private selection record lacks any reader, artifact, source-export/control-receipt verifier, archive-custodian verifier, owner confirmation, owner-entitlement verifier, transport-supervisor, or rollback/recovery readiness binding, has invalid signer trust, or mismatches the running image
- **THEN** startup does not advertise or admit v5
- **AND** it neither infers promotion nor changes configured v1 through v4 behavior

#### Scenario: Unknown action is invoked

- **WHEN** `consolidate_memory` receives an omitted, unknown, or unclassified action
- **THEN** it fails closed with a stable validation error naming the finite actions
- **AND** it creates no run, receipt, token, artifact, seal, or canonical mutation

#### Scenario: Caller tries to submit archive bytes

- **WHEN** any surface submits an inline archive, private filesystem path, verifier key, or serialized internal authority
- **THEN** request validation rejects the field before engine dispatch
- **AND** the response does not echo the supplied secret or path

### Requirement: Action classification is conservative and shared by local and Hosted admission

`invocation_is_read_only` SHALL resolve the `consolidate_memory.action` selector
and return read-only only for `status`. `start`, `reconcile`, `plan`, `approve`,
`apply`, `verify`, `recover`, `abort`, `rollback`, and `retire-source` SHALL all be
mutations because they create or change durable run state, evidence, authority,
policy, content, or lifecycle state. Mutating actions SHALL enter mutation
identity, idempotency, terminal-response, and writer authority. Ordinary
run-state mutations MAY use normal writer admission, but apply and an identical
tagged-request/operation-id retry of that apply,
verify, recover, abort, and rollback SHALL use the consolidation owner-control
mutation lane rather than an ordinary full-leaf Hosted mutation wrapper, so
seal drain cannot count/wait on itself. Unknown or omitted selectors SHALL never
default to read-only.

The mixed command's MCP and generated capability annotations SHALL advertise it
conservatively as write-capable even though `status` dispatches lease-free. The
same selector classifier SHALL govern local writer-lease admission and Hosted
read/write admission; Hosted SHALL NOT maintain a bespoke classification table.

#### Scenario: Every action is classified

- **WHEN** selector classification is tested for all eleven actions plus omitted and unknown values
- **THEN** only `status` is read-only
- **AND** every other or unrecognized invocation is write-capable or refused before dispatch

#### Scenario: Status reads durable state

- **WHEN** an authorized owner invokes `status` for a run
- **THEN** it reads bounded durable state without acquiring writer authority or creating a receipt
- **AND** an ordinary or unresolved principal receives no private run detail

#### Scenario: Hosted invokes plan

- **WHEN** Hosted invokes `plan`
- **THEN** the shared classifier routes it through writer admission and idempotency exactly as local MCP, REST, and CLI do
- **AND** Hosted cannot treat it as a read merely because it publishes no corpus file

### Requirement: Trusted identity and confirmation context is injected by each surface

Every `consolidate_memory` action, including `start`, `status`, `reconcile`,
`plan`, `approve`, `apply`, `verify`, `recover`, `abort`, `rollback`, and
`retire-source`, SHALL require an authenticated destination owner at the control
plane before resolving a run, artifact, source, destination, or private identity
or allocating inventory/staging work. Authentication failure SHALL be
content-free and SHALL create no run, receipt, artifact, token, seal, mutation,
or computationally expensive import path. An agent operating under an
authenticated owner connection MAY invoke calculation/proposal actions, but it
SHALL not thereby acquire the distinct human confirmation needed for approval,
drift-reconciled rollback, or source retirement.

The verified owner context SHALL conform to
`ConsolidationOwnerContext/v1` and bind logical vault id, installation id/
generation and active-fence digest, canonical principal, authorization session, purpose
`vault-consolidation`, exact allowed action, issuer/surface family,
issued/expiry, nonce, and verifier fingerprint. It SHALL be obtained before
body-dependent coercion or work, and request content SHALL not select or widen
it. Before that check an adapter MAY perform only bounded envelope decoding of
`schema` and `action` needed to select the authority purpose; it SHALL not coerce,
dereference, validate existence, hash, or allocate for any action field. Hosted SHALL accept only a validated gateway/control-plane owner-entitlement
assertion bound to trusted cell context. CLI SHALL verify the actual local
OS/vault owner and authenticated TTY or protected out-of-band capability; it
SHALL NOT infer owner from an unbound library call or `local_owner()` default.
MCP/REST SHALL use their trusted principal/session resolution. Invalid,
wrong-action, cross-vault/installation/session/issuer, or expired context SHALL
produce equivalent content-free denial.

Hosted routing `cell_id` SHALL be resolved from validated gateway/control-plane
state and SHALL remain a distinct typed value from logical `vault_id` and
`installation_id`. A gateway assertion or private binding that aliases
`cell_id == vault_id`, omits the installation generation/fence, or lets the body
select any of those identities SHALL fail before owner-context construction and
before run existence is tested.

Every surface SHALL resolve the authenticated destination vault and canonical
principal outside the request body. `approve` SHALL require an injected trusted
owner-confirmation capability plus the caller-visible expected plan digest.
`retire-source` and any newly reconciled post-unseal rollback plan SHALL require
their own purpose-bound injected owner confirmation. Caller-controlled identity,
role, audience, owner, `approved`, confirmation, or internal-capability fields
SHALL NOT create or upgrade authority.

CLI SHALL obtain owner confirmation from an authenticated owner TTY and SHALL
fail closed in non-interactive mode unless an already-issued purpose-bound
out-of-band confirmation is supplied by the trusted host seam. Hosted SHALL use
its authenticated confirmation route. MCP and REST SHALL use host
confirmation/elicitation where available or the same out-of-band owner
confirmation mechanism. Surface differences SHALL affect only acquisition of
trusted context; the resulting engine preimage, digest, token, and state
transition SHALL be identical.

#### Scenario: Agent sets approved in request JSON

- **WHEN** an MCP or REST caller includes an `approved`, `owner`, audience, or internal-capability value in request content
- **THEN** the value is rejected or ignored as untrusted input and cannot mint an approval token
- **AND** approval succeeds only when the adapter separately injects authenticated purpose-bound owner confirmation

#### Scenario: Non-owner tries to start or inspect a run

- **WHEN** a non-owner or unresolved principal invokes any action, including `start` or `status`, with valid-looking artifact or run identifiers
- **THEN** authorization refuses before existence lookup, parsing, inventory, staging, or writer work
- **AND** no response or side effect reveals whether the run, artifact, source, or destination exists

#### Scenario: Agent proposes under an owner connection

- **WHEN** an agent invokes reconcile or plan through a currently authenticated owner control session
- **THEN** the deterministic proposal may be persisted under that owner's authority
- **AND** the agent still cannot mint the separate human confirmation for apply, a drift-reconciled rollback, or retirement

#### Scenario: CLI approval has no interactive owner

- **WHEN** `consolidate-memory --action approve` runs without an authenticated interactive owner TTY or a valid trusted out-of-band confirmation
- **THEN** it fails without issuing a token
- **AND** a `--yes`-style caller flag alone cannot bypass confirmation

#### Scenario: Hosted cell and vault identifiers alias

- **WHEN** the gateway owner-entitlement assertion uses one value as both routing `cell_id` and logical `vault_id`
- **THEN** the adapter rejects the assertion before parsing action-specific refs or looking up a run
- **AND** the request cannot turn a deployment identity into owner authority

### Requirement: Every action is one closed tagged request with explicit retry identity

Generated and runtime validation SHALL use the same closed discriminated union
`exomem.consolidate-memory-request/v1`. Every variant SHALL require `schema` and
`action`, reject unknown/duplicate/null/cross-action fields, accept NFC bounded
strings and canonical lowercase UUIDv4 ids, and treat artifact/token/attestation
references as opaque non-path strings. Every mutation SHALL require a distinct
`operation_id`; MCP/REST/Hosted callers SHALL supply it, while CLI MAY generate
one only if it durably prints/persists it before the first request. Unkeyed
mutation SHALL be refused. Every existing-run mutation SHALL require `run_id`
and exact integer `expected_run_revision` in 0..2^53-1; `start` SHALL require
revision 0.

`operation_id`, `run_id`, and every `*_operation_id` request field SHALL be
canonical lowercase UUIDv4;
token JTIs never occur as caller-selected raw fields. Every `*_digest` and
`expected_intent_event_id` SHALL be exactly 64 lowercase hexadecimal SHA-256 characters.
`successor_context_ref` SHALL be an opaque owner-control ref and
`successor_context_digest` SHALL be exactly 64 lowercase hex; explicit
predecessor, basis, publication-state, original-apply, or contingency-authority
fields SHALL be rejected in a branch that uses that protected context.
Every opaque `*_ref`, cursor, and render-session reference SHALL be 1..512 UTF-8
bytes, contain no NUL, and SHALL not be interpreted as a filesystem path. Enum
spellings shown below are exhaustive. Revisions, page ordinals, and limits SHALL
be JSON integers, never strings or floats; limits are 1..200, page ordinals are
0..2^31-1, and revisions are 0..2^53-1.

| Action | Required fields beyond `schema`,`action` | Optional/conditional fields | Forbidden |
|---|---|---|---|
| `start` | `operation_id`, `expected_run_revision=0`, `run_mode={cloned-rehearsal,real-cutover}`, `source_artifact_ref`, `source_attestation_ref` | `clone_binding_ref` iff rehearsal | `run_id`, decisions, policy, tokens |
| `status` | `run_id` | `expected_run_revision`, `detail={summary,owner-detail}` (default `summary`), `cursor`, `limit` 1..200 (default 50) | `operation_id`, mutation fields |
| `reconcile` | `operation_id`, `run_id`, `expected_run_revision`, `expected_inventory_digest`, `decision_set_ref`, `decision_set_digest` | none | inline/free-form decisions |
| `plan` | `operation_id`, `run_id`, `expected_run_revision`, `plan_kind={cutover,rollback,retirement}`, `operation={materialize,render}`, `successor_context_ref`, `successor_context_digest` | materialize: exactly one closed kind-options object; render: `plan_digest`, `render_step={begin,page,acknowledge,complete}`, and step-required `page_ordinal`, `render_session_ref`, `acknowledged_page_digest` | caller plan/sections/pages; multiple kind options; explicit predecessor/basis fields |
| `approve` | `operation_id`, `run_id`, `expected_run_revision`, `plan_kind={cutover,rollback,retirement}`, `plan_digest`, `rendering_completeness_ref`, `rendering_completeness_digest`, `successor_context_ref`, `successor_context_digest` | none | caller display/summary, approval boolean, explicit predecessor fields |
| `apply` | `operation_id`, `run_id`, `expected_run_revision`, `cutover_plan_digest`, `approval_token_ref`, `approval_token_digest`, `successor_context_ref`, `successor_context_digest` | none | other-kind token, resume flag, changed retry, explicit predecessor fields |
| `verify` | `operation_id`, `run_id`, `expected_run_revision`, `verification_kind={in-process,transport}`, `expected_plan_digest`, `expected_verification_basis_digest` | none | authority/principal override |
| `recover` | `operation_id`, `run_id`, `expected_run_revision`, `expected_journal_digest` | `expected_intent_event_id` | new decisions/plan/artifact |
| `abort` | `operation_id`, `run_id`, `expected_run_revision`, `expected_journal_digest`, `reason_code={owner-cancelled,preimage-unavailable,verification-failed,maintenance-window-expired}` | owner-only `reason` <=500 UTF-8 bytes | at/after publication boundary |
| `rollback` | common: `operation_id`,`run_id`,`expected_run_revision`,`rollback_mode={nonterminal-contingency,terminal-plan}`,`successor_context_ref`,`successor_context_digest`; terminal only: `rollback_plan_digest`,`rollback_token_ref`,`rollback_token_digest` | none | nonterminal forbids original-apply/journal/publication/contingency-authority plus rollback-plan/token fields and implicit target; terminal forbids original-apply/contingency fields; both forbid explicit predecessor fields, resume flag, and other-kind token |
| `retire-source` | clearance: `operation_id`,`run_id`,`expected_run_revision`,`phase=clearance`,`retirement_plan_digest`,`retirement_token_ref`,`retirement_token_digest`,`successor_context_ref`,`successor_context_digest`; finalize: `operation_id`,`run_id`,`expected_run_revision`,`phase=finalize`,`retirement_plan_digest`,`retirement_lifecycle_ref`,`completion_attestation_ref`,`completion_attestation_digest` | none | clearance forbids lifecycle/completion and explicit predecessor fields; finalize forbids token/successor-context fields; both forbid source bytes/credentials and reusable report semantics |

For `plan(operation="materialize")`, exactly the options object matching
`plan_kind` SHALL be present and every render field SHALL be forbidden. For
`plan(operation="render")`, kind-options objects SHALL be forbidden and:

| `render_step` | Additional required fields | Forbidden render fields |
|---|---|---|
| `begin` | `plan_digest` | `render_session_ref`, `page_ordinal`, `acknowledged_page_digest` |
| `page` | `plan_digest`, `render_session_ref`, `page_ordinal` | `acknowledged_page_digest` |
| `acknowledge` | `plan_digest`, `render_session_ref`, `page_ordinal`, `acknowledged_page_digest` | none |
| `complete` | `plan_digest`, `render_session_ref` | `page_ordinal`, `acknowledged_page_digest` |

`begin` SHALL return the trusted `render_session_ref`; later steps SHALL bind the
same owner/session/surface and stored plan. Every transition SHALL resolve the
request's `successor_context_ref`/digest through owner-only control state. The
closed JCS object `exomem.consolidation-successor-context/v1` SHALL have exactly
`schema`, `context_kind`, `run_id`, `run_revision`,
`destination_binding_digest`, `owner_binding_digest`, `basis_digest`,
`context_seed_digest`,
`predecessor_event_id`, `predecessor_payload_digest`, `successor_action`,
`successor_variant`, `issued_at`, `expires_at`, `nonce`, and `facts`.
`predecessor_event_id` SHALL be `<64-lowercase-hex>:committed`; every digest is
64 lowercase hex; revision is 0..2^53-1; and `issued_at < expires_at`.

The tagged `context_kind` branches and their exact closed `facts` objects SHALL
be:

| Context kind | Exact successor | Exact `facts` fields |
|---|---|---|
| `plan-materialize` | `successor_action=plan`, `successor_variant=materialize` | `eligible_plan_kinds`, `plan_input_basis_digest` |
| `render-begin` | `plan`, `render-begin` | `plan_kind`, `plan_digest` |
| `render-page` | `plan`, `render-page` | `plan_kind`, `plan_digest`, `render_session_digest`, `page_ordinal` |
| `render-acknowledge` | `plan`, `render-acknowledge` | `plan_kind`, `plan_digest`, `render_session_digest`, `page_ordinal`, `page_digest` |
| `render-complete` | `plan`, `render-complete` | `plan_kind`, `plan_digest`, `render_session_digest` |
| `approve` | `approve`, with successor variant exactly equal to `plan_kind` | `plan_kind`, `plan_digest`, `rendering_completeness_digest` |
| `apply` | `apply`, `cutover` | `cutover_plan_digest`, `approval_token_digest` |
| `rollback-terminal-plan` | `rollback`, `terminal-plan` | `rollback_plan_digest`, `rollback_token_digest` |
| `retire-source-clearance` | `retire-source`, `clearance` | `retirement_plan_digest`, `retirement_token_digest` |
| `rollback-nonterminal-contingency` | `rollback`, `nonterminal-contingency` | `original_apply_operation_id`, `original_apply_journal_digest`, `cutover_plan_digest`, `rollback_contingency_digest`, `publication_state_digest`, `contingency_authority_ref`, `contingency_authority_digest`, `recovery_window_deadline` |

`eligible_plan_kinds` is a nonempty, duplicate-free array in the fixed subset
order `cutover`, `rollback`, `retirement`; `plan_kind` is exactly
`cutover|rollback|retirement`; page ordinal is
0..2^31-1; the original apply operation is UUIDv4; the recovery deadline uses
the common timestamp contract; and no branch accepts an unlisted fact. The
context digest SHALL be SHA-256 over the common frame with ASCII domain
`exomem.consolidation-successor-context/v1` and those closed JCS bytes; the
opaque ref/digest remain outside the object. The full object SHALL be derived
only after its committed predecessor under the seed contract below. Resolution
SHALL recompute `context_seed_digest`, then verify current owner/destination/run/
revision, expiry, basis, current committed predecessor and nested payload digest,
action/variant, and every request-visible plan/session/page/token digest. It
SHALL reject inline context, a request-supplied protected
fact, a foreign owner binding, a foreign render-session/surface binding for a
render branch, or a digest/reference mismatch.

`destination_binding_digest` SHALL equal the authenticated identity/root-
binding fingerprint in the destination snapshot. `owner_binding_digest` SHALL
be framed-JCS SHA-256 under ASCII domain
`exomem.consolidation-successor-owner-binding/v1` over a closed object with
exactly `schema`, `vault_id`, `installation_id`, `generation`,
`active_fence_digest`, `principal_digest`, and `purpose=vault-consolidation`,
derived from the validated owner context; `schema` SHALL equal that same
versioned domain string. `basis_digest` SHALL equal `plan_input_basis_digest`
for `plan-materialize`, the relevant stored plan's `control_basis_digest` for
each render/approve/apply/terminal-rollback/retirement-clearance context, and
`original_apply_journal_digest` for nonterminal contingency. None of those
digests or `context_seed_digest` is nullable or caller-selected. The stable
owner binding omits session/surface so a newly
authenticated equivalent adapter can continue/retry; render-session facts
separately enforce one trusted session/surface during page coverage.

Configured `successor_context_ttl_ms` SHALL be an integer 1..86,400,000.
`plan-materialize.expires_at` SHALL be exactly
`9999-12-31T23:59:59.999Z`; as a non-bearer current-state witness it remains
subject to fresh owner/current-predecessor/basis revalidation, and an allowed
pending state cannot become unreachable merely by time. A stored-plan review or
approved-executor context SHALL use the earlier of `issued_at + ttl` and its
bound plan/completeness/token deadline; nonterminal contingency SHALL use the
earlier of that value and the recovery-window deadline. Status SHALL NOT extend
expiry. After a non-contingency child expires, no action may continue it; a new
plan-entry pair MAY arise only from a later product terminal explicitly listed
in the table, and status cannot mint one.

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

The first successor operation SHALL reserve the context pair with its operation
id/request digest. Only its byte-identical retry may reuse that context; another
operation or any unexpected same-run successor makes it stale. The context is a
state witness, not a bearer capability. Only the exact plan-entry table above
SHALL install or return a `plan-materialize` context with its exact nonempty
eligible-kind set. Plan materialization, every render step, render-complete, and
approval SHALL durably create the one exact next review/executor context before
returning it; any nonrow plan-entry terminal returns no context and cannot
advertise `plan`.
Owner-detail status SHALL return the existing
current pair without minting, refreshing, consuming, or emitting an event. A
sealed nonterminal apply context SHALL keep its original apply/journal,
publication-state, cutover/contingency, protected authority ref/digest, current
predecessor, and deadline server-side; the rollback request carries only the
opaque context pair.

The plan SHALL bind immutable framed-JCS
`exomem.consolidation-control-basis/v1` and
`exomem.consolidation-plan-successor-automaton/v1` digests. The control basis
binds basis revision, immutable plan inputs, plan nonce, materialization
operation, and the predecessor resolved from the materialize context. The
automaton object SHALL contain exactly `schema`, `initial_state`, `states`,
`terminal_state`, `minimum_pages`, `page_count_source`, `transitions`,
`retry_rule`, and `unexpected_event_rule`. Its fixed values SHALL be:

- `schema=exomem.consolidation-plan-successor-automaton/v1`,
  `initial_state=plan-materialized`, `terminal_state=token-reservation`,
  `minimum_pages=1`, and `page_count_source=stored-plan-rendering-definition`;
- ordered `states=[plan-materialized,render-begin,render-page,render-ack,
  render-complete,approval,token-reservation]`;
- seven ordered transition objects with exactly `ordinal`, `from_state`,
  `to_state`, `guard`: `(0,plan-materialized,render-begin,once)`,
  `(1,render-begin,render-page,page-0)`,
  `(2,render-page,render-ack,same-page)`,
  `(3,render-ack,render-page,next-page-if-any)`,
  `(4,render-ack,render-complete,last-page)`,
  `(5,render-complete,approval,complete-coverage)`, and
  `(6,approval,token-reservation,matching-kind-token)`; and
- `retry_rule=adopt-existing-identical-event` and
  `unexpected_event_rule=stale-plan`.

The automaton digest SHALL be framed-JCS SHA-256 under its schema's exact ASCII
domain and outside the object. The protocol-static object SHALL contain no run,
plan, page, control-basis, or self-digest; the plan separately binds the ordered
render definition/page count. For that count it permits only materialized,
begin, ordered page/ack pairs, complete, approval, and token reservation. It
ends at token reservation. Apply, terminal-plan rollback, and retirement
clearance resolve their approval context, commit the matching token-reservation
terminal, and bind that terminal separately as the first executor-effect
predecessor. Apply's `seal-intent` is the first apply effect; no additional
review state/event lies between token reservation and that intent. Status creates
no semantic-successor event but owner-detail may return the already-durable
current context pair; unrelated physical
receipt appends may interleave, and another same-run event/order/predecessor
stales the plan without comparing the mutable global receipt head/run tree to
materialization.

Retirement clearance SHALL consume the retirement-token JTI into the returned
opaque `retirement_lifecycle_ref`; finalize SHALL use that exact lifecycle
journal and SHALL not consume the token twice. Nonterminal rollback SHALL
revalidate every protected context fact and dereference the one purpose-bound
contingency reservation; it accepts no rollback token and that authority expires
with terminal apply completion. Apply/rollback recovery SHALL replay the
identical original tagged request and operation id; a body `resume` field is
forbidden.

For `render_step=acknowledge`, the caller field is only an expected-page check.
The adapter SHALL separately inject a non-caller-selected
`PlanRenderAcknowledgement/v1` from the trusted human surface binding owner,
authorization session, issuer/surface, render session, run, plan kind/digest,
section id, page ordinal/digest, impact-summary digest, issued time, and nonce.
Without that exact context the mutation records no acknowledgement; an agent or
body field alone cannot advance coverage. `complete` derives completeness only
from those durable trusted acknowledgements.

The materialize kind-options objects SHALL have these exact fields; unknown,
missing, inapplicable, or additional fields SHALL be refused:

| Object | Required fields | Conditional fields |
|---|---|---|
| `cutover_options` | `expected_reconciliation_digest`, `expected_policy_bundle_digest`, `expected_principal_attestation_set_digest`, `expected_verification_plan_digest`, `expected_rollback_contingency_digest`, `expected_source_retention_digest`, `expected_control_basis_digest` | `expected_rehearsal_proof_digest` iff real-cutover |
| `rollback_options` | `target_kind={pre-cutover,post-cutover-forward}`, `target_snapshot_ref`, `target_snapshot_digest`, `expected_current_census_digest`, `treatment_set_ref`, `treatment_set_digest`, `surviving_copy_ledger_digest`, `expected_retirement_state_digest` | none |
| `retirement_options` | `archive_disposition={retain,transfer,external-destruction}`, `archive_artifact_ref`, `archive_artifact_digest`, `archive_terms_digest`, `post_retirement_rollback_mode={pre-cutover-reversible,forward-only}`, `source_retention_proof_ref`, `source_retention_proof_digest`, `surviving_copy_ledger_digest`, `retained_irrecoverable_statement_digest` | transfer requires `custodian_receipt_ref`,`custodian_receipt_digest`; forward-only requires `forward_snapshot_ref`,`forward_snapshot_digest` |

Every digest/ref uses the common bounds above. For external destruction,
source-retention proof describes the current copy only and SHALL not satisfy the
post-effect ledger; for forward-only, the forward snapshot must. Inapplicable
custodian/forward fields are forbidden.

For transfer, `custodian_receipt_ref` SHALL resolve only through the private
artifact/trust seam to detached `archive-custody-receipt/v1`; the body cannot
supply receipt bytes or verifier trust. Its digest SHALL bind custodian identity
and retention domain, source vault/installation/generation, archive/manifest/
source-census digests, transfer operation, retention-terms digest, validity,
nonce, and signer key id. Clearance, source consumption, rollback-plan
materialization, and rollback commit SHALL revalidate the configured
`ArchiveCustodianVerifierRecord/v1` and exact retained artifact before counting
that custody as a survivor.

Cutover options SHALL bind exact reconciliation/policy/verification/retention
digests; rollback options SHALL bind target, current census, treatment-set
ref/digest, and surviving-copy ledger; retirement options SHALL bind archive
disposition/proof, post-retirement rollback mode, required forward snapshot, and
retained-versus-irrecoverable statement. Generated JSON Schema SHALL express
these as `oneOf` branches and runtime SHALL enforce the identical tagged union.

Every success SHALL use `exomem.consolidation-terminal/v1` with exact required
fields `schema`, `action`, `outcome`, `run_id`, `run_revision`, `phase`,
`artifact_digests`, `counts`, `next_actions`, and `trusted_outputs`. Every
mutation SHALL additionally require `operation_id`, canonical `request_digest`,
`prior_state_digest`, and `final_state_digest`, and SHALL set
`outcome=committed`. Status SHALL set `outcome=observed` and forbid those four
mutation fields. `replayed` SHALL not be a logical outcome. Every successful
MCP, REST, CLI, and Hosted response SHALL be the exact closed object
`{"success":true,"data":{"delivery":D,"terminal":T}}`, where `D` is exactly
the enum string `initial` or `replayed`, `T` is the logical terminal above, the
top level has exactly `success` and `data`, and `data` has exactly `delivery`
and `terminal`. Idempotency stores the canonical JCS bytes of `T` only; an
adapter constructs the envelope after lookup. First delivery and every status
response use `initial`; only a mutating reservation that returns its already
stored terminal uses `replayed`. A lost-ack retry therefore changes only `D`
and returns byte-identical canonical bytes for `T`. Stable errors SHALL use the
existing shared content-free failure envelope and never this success schema.

`artifact_digests`, `counts`, and `trusted_outputs` SHALL be closed objects.
Every digest and count key listed in the selected row is required, every
unlisted key is forbidden, and every count is an integer 0..2^53-1. `{}` means
an exact empty object. `next_actions` SHALL always be present as a duplicate-free
array in the listed order. It SHALL contain the exact currently phase-eligible
subset of the row's closed enum; phase-to-subset transition fixtures are
generated from the same state machine, and no unlisted action string is valid.

| Terminal branch | Exact required `artifact_digests` keys | Exact required `counts` keys | Closed ordered `next_actions` enum | Exact `trusted_outputs` |
|---|---|---|---|---|
| `start` | `source_fingerprint`, `destination_snapshot_fingerprint` | `source_objects`, `source_bytes`, `destination_objects`, `destination_bytes` | `status`, `reconcile` | `{}` |
| `status` | `run_state_digest`, `journal_digest` | `completed_effects`, `pending_effects`, `blocked_effects`, `warnings` | `status`, `reconcile`, `plan`, `approve`, `apply`, `verify`, `recover`, `abort`, `rollback`, `retire-source` | `next_cursor` iff another requested page exists; `successor_context_ref`,`successor_context_digest` together iff `detail=owner-detail` and a current successor context exists; otherwise absent |
| `reconcile` | `inventory_digest`, `reconciliation_digest`, `mapping_set_digest` | `c1`, `c2`, `c3`, `c4`, `c5`, `c6`, `c7`, `c8`, `unresolved`, `mappings` | `status`, `reconcile`, `plan` | required pair iff this exact `phase=reconcile` terminal satisfies at least one plan-entry row; `eligible_plan_kinds` is exactly that ordered row set; otherwise `{}` |
| `plan:materialize` | `plan_digest`, `control_basis_digest`, `plan_successor_automaton_digest` | `content_actions`, `policy_documents`, `impact_rows`, `render_pages` | `status`, `plan` | required `successor_context_ref`,`successor_context_digest` for `plan/render-begin` |
| `plan:render-begin` | `plan_digest`, `render_session_digest` | `render_pages`, `acknowledged_pages` | `status`, `plan` | required `render_session_ref`,`successor_context_ref`,`successor_context_digest` for `plan/render-page` |
| `plan:render-page` | `plan_digest`, `render_page_digest` | `page_ordinal`, `page_rows`, `render_pages` | `status`, `plan` | required `render_session_ref`,`render_delivery_ref`,`successor_context_ref`,`successor_context_digest` for `plan/render-acknowledge` |
| `plan:render-acknowledge` | `plan_digest`, `render_ack_digest` | `acknowledged_pages`, `render_pages` | `status`, `plan` | required `render_session_ref`,`successor_context_ref`,`successor_context_digest`; successor is `plan/render-page` unless the acknowledged page is last, then `plan/render-complete` |
| `plan:render-complete` | `plan_digest`, `rendering_completeness_digest` | `acknowledged_pages`, `render_pages` | `status`, `approve` | required `rendering_completeness_ref`,`successor_context_ref`,`successor_context_digest` for `approve/<plan_kind>` |
| `approve` | `plan_digest`, `approval_token_digest` | `acknowledged_pages`, `render_pages` | cutover: `status`,`apply`; rollback: `status`,`rollback`; retirement: `status`,`retire-source` | required `approval_token_ref`,`successor_context_ref`,`successor_context_digest`; successor is respectively `apply/cutover`, `rollback/terminal-plan`, or `retire-source/clearance` |
| `apply` | `cutover_terminal_digest`, `post_cutover_census_digest`, `apply_predecessor_digest` | `policy_documents`, `content_batches`, `content_actions`, `rebuild_kinds`, `in_process_probes`, `transport_probes` | `status`, `plan`, `apply`, `verify`, `recover`, `abort`, `rollback`, `retire-source` | at exact `phase=complete`, required `plan-materialize` pair with `rollback` and also `retirement` iff its table eligibility holds; before terminal completion, required `rollback-nonterminal-contingency` pair iff the sealed contingency is eligible; otherwise `{}` |
| `verify` | `verification_basis_digest`, `verification_terminal_digest` | `positive_probes`, `negative_probes`, `passed_probes`, `failed_probes` | `status`, `plan`, `apply`, `verify`, `recover`, `rollback`, `retire-source` | at exact `verification_kind=transport,phase=complete`, same plan-entry pair as completed `apply`; before terminal completion, required nonterminal-contingency pair iff eligible; every other verify terminal uses `{}` |
| `recover` | `journal_digest`, `recovery_terminal_digest` | `classified_effects`, `repaired_effects`, `blocked_effects` | `status`, `plan`, `apply`, `verify`, `recover`, `abort`, `rollback`, `retire-source` | at exact `phase=repair-terminal`, required plan-entry pair iff its committed/adopted target satisfies a table row, with exactly those eligible kinds; otherwise required nonterminal-contingency pair iff the resulting sealed apply is eligible; every other recovery terminal uses `{}` |
| `abort` | `prior_census_digest`, `abort_terminal_digest` | `restored_entries`, `removed_candidates`, `evidence_events` | `status` | `{}` |
| `rollback:nonterminal-contingency` | `cutover_plan_digest`, `original_apply_journal_digest`, `rollback_contingency_digest`, `target_census_digest`, `rollback_terminal_digest` | `restored_entries`, `retained_entries`, `reapplied_entries`, `discarded_entries`, `rebuild_kinds`, `verification_probes` | `status`, `plan`, `recover`, `verify`, `rollback` | at exact `phase=rollback-complete`, required `plan-materialize` pair with only `rollback` eligible |
| `rollback:terminal-plan` | `rollback_plan_digest`, `target_census_digest`, `rollback_terminal_digest` | `restored_entries`, `retained_entries`, `reapplied_entries`, `discarded_entries`, `rebuild_kinds`, `verification_probes` | `status`, `plan`, `recover`, `verify`, `rollback` | at exact `phase=rollback-complete`, required `plan-materialize` pair with only `rollback` eligible |
| `retire-source:clearance` | `retirement_plan_digest`, `clearance_digest`, `retirement_lifecycle_digest`, `surviving_copy_ledger_digest` | `survivor_rows`, `verified_survivor_rows` | `status`, `retire-source` | required `retirement_clearance_ref`, `retirement_lifecycle_ref` |
| `retire-source:finalize` | `retirement_plan_digest`, `retirement_lifecycle_digest`, `completion_digest`, `finalization_digest`, `surviving_copy_ledger_digest` | `completion_records`, `survivor_rows` | `status`, `plan` | at exact terminal phase `retirement-finalize`, required `plan-materialize` pair with only `rollback` eligible |

For status, `next_cursor` is independent, while the two successor-context keys
always appear together; no other combination is serializable. Every conditional
row above follows the same pair-or-empty rule. A product terminal includes
`plan` in `next_actions` exactly when it returns a table-authorized
`plan-materialize` pair; a nonterminal-contingency context never advertises
`plan`. All trusted refs
are opaque 1..512-byte values delivered only through
the authenticated owner/control adapter and never receipts. A successor context
resolves server-side to protected predecessor, publication, and contingency
authority facts and is not itself a bearer capability. `render_delivery_ref`
is dereferenced only by
the trusted surface to display the exact stored page to the human; page/body
bytes do not enter an agent-facing logical terminal, retry record, log, or
receipt. CLI renders it to its authenticated TTY, Hosted through confirmation,
and MCP/REST through trusted elicitation/OOB. Corresponding digests keep
acknowledgement and logical-terminal replay exact.

Cross-surface reservation identity SHALL bind logical vault,
installation/generation, verified owner principal, and operation id. Its stored
record SHALL bind action and canonical tagged-request digest; that digest SHALL
exclude transport wrappers but the durable identity SHALL bind owner context
separately. A lost acknowledgement MAY replay across surfaces
only for the same newly verified owner and byte-identical request. Reusing an
operation id with changed action/request/plan/token/binding/owner SHALL return
`OPERATION_CONFLICT`. A later intentional identical action SHALL use a new id.

#### Scenario: Every mutation loses acknowledgement

- **WHEN** each of the ten mutating actions and each conditional mutating branch, including every plan render step and both retirement phases, commits and is retried through another surface with the same operation id, owner, and canonical tagged request
- **THEN** the initial response is exactly `{"success":true,"data":{"delivery":"initial","terminal":T}}` and each retry is exactly `{"success":true,"data":{"delivery":"replayed","terminal":T}}`, with byte-identical canonical `T` and `outcome=committed`
- **AND** changed input conflicts while an unkeyed mutation is refused

#### Scenario: Returned contexts make every protected successor reachable

- **WHEN** an eligible zero-unresolved reconcile, materialize, begin, every ordered page/ack pair, complete, and approval progress by passing each returned successor-context ref/digest into the next tagged request
- **THEN** each terminal exposes exactly the owner-only context required by that next request, the immutable control basis remains an ancestor, and apply binds/revalidates its token-reservation predecessor separately for `seal-intent`
- **AND** unrelated-run physical receipt appends do not stale the plan or require the global receipt head to equal its materialization value

#### Scenario: Status recovers a protected successor after lost delivery

- **WHEN** delivery of an eligible reconciliation, review, approval, terminal planning-eligibility, or sealed nonterminal-contingency terminal is lost and the owner requests `status(detail=owner-detail)`
- **THEN** status returns the already-durable current `successor_context_ref` and digest without minting a replacement or exposing predecessor, publication, or contingency-authority facts
- **AND** summary status and non-owner calls expose neither key

#### Scenario: Plan-entry output producers are table-closed

- **WHEN** terminal serialization is exercised for every listed plan-entry producer and every nonrow action/phase/mode/repair target/run mode/eligibility combination
- **THEN** each listed producer returns one durable pair with the exact applicable `eligible_plan_kinds` and includes `plan`, while status returns it only in owner-detail
- **AND** each nonrow uses `{}` or only its separately eligible nonterminal context, omits `plan`, and cannot serialize a plan-materialize pair

#### Scenario: Successor-context derivation is acyclic

- **WHEN** a context-producing mutation is interrupted after seed reservation, receipt terminal, journal final, or logical-terminal storage
- **THEN** recovery follows the fixed `S -> terminal payload digest -> full context` derivation and returns the pair only after the full context is journal-final
- **AND** no receipt or target/observed digest contains the full context commitment, while a seed, terminal, context, ref, or idempotency mismatch refuses successor admission

#### Scenario: Unexpected same-run event interrupts review

- **WHEN** a render page is skipped/reordered/replayed as a new event, a request presents the wrong/stale context, or reconcile/new-plan/another same-run control event intervenes
- **THEN** the successor automaton marks the stored plan stale before approval, token reservation, or execution
- **AND** retry may adopt only an existing deterministic identical event rather than adding a duplicate successor

#### Scenario: Nonterminal rollback selects its explicit branch

- **WHEN** rollback selects `nonterminal-contingency` with the returned successor context for the exact still-sealed apply journal, publication state, contingency, current predecessor, and reserved authority
- **THEN** it executes without a terminal rollback plan/token and returns the nonterminal-contingency terminal branch idempotently
- **AND** supplying both branch field sets, using the authority after apply terminal, or changing any bound value is refused before restore

#### Scenario: Tagged union receives cross-action fields

- **WHEN** a request supplies two kind-options objects, a rollback target to apply, a token to status, or another field outside its selected action branch
- **THEN** generated-schema and runtime validation both refuse before owner-authorized work
- **AND** no adapter silently ignores the foreign field

### Requirement: Generated schemas and parity gates cover exact consolidation behavior

Schema-fidelity tests SHALL intentionally add only the new
`consolidate_memory` tool schema and any separately enumerated bootstrap/help
copy changes to the committed generated baselines. Contract tests SHALL prove
identical action validation, normalized result fields, stable error codes,
content-free sealed outcomes, idempotent retry terminals, and trusted-context
handling across MCP, REST, CLI, and v5 Hosted. OpenAPI and generated capability
documentation SHALL describe the same finite schemas, owner-inclusive seal,
three distinct approvals, and Exomem-mediated enforcement boundary.

#### Scenario: Generated schema changes are reviewed

- **WHEN** schema, OpenAPI, Hosted manifest, bootstrap, and capability fixtures are regenerated
- **THEN** the gate reports an explicit bounded diff attributable to consolidation and the new v5 artifacts
- **AND** unrelated command schemas/descriptions and all v1 through v4 artifacts remain byte-identical

#### Scenario: Success wrapper fixtures are exact

- **WHEN** generated and runtime fixtures serialize first delivery, status, and a stored-terminal replay for every action branch on MCP, REST, CLI, and Hosted
- **THEN** each success has exactly top-level `success=true` and `data`, while `data` has exactly `delivery` and `terminal`; first delivery/status use `initial` and only stored mutation adoption uses `replayed`
- **AND** flattening `delivery`, omitting either nested key, adding any wrapper key, using another delivery value, or changing canonical terminal `T` on replay fails schema and runtime validation

#### Scenario: Same refusal crosses every surface

- **WHEN** each surface applies the same authenticated input to an unresolved-conflict, stale-plan, sealed-destination, or changed-retry case
- **THEN** every adapter returns the same stable logical code and equivalent content-free data
- **AND** no adapter leaks a path, title, snippet, policy fact, run phase, item existence, or principal-specific secret through its envelope
