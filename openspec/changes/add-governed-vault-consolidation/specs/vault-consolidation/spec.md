## ADDED Requirements

### Requirement: Consolidation intake is authenticated, content-addressed, and immutable

The system SHALL start a consolidation run only from an existing versioned
portable archive referenced by an opaque artifact identifier. It SHALL NOT
accept an inline archive, infer completeness from a live MCP crawl, or restore
the archive over the active destination root. Before inventory, it SHALL verify
the archive manifest, every manifested entry digest, the canonical source census,
and the source quiescence checkpoint.

Archive self-consistency SHALL NOT establish source identity. Admission SHALL
also require either an authenticated transport receipt from a configured trusted
source/control-plane channel with an equally exact issuer record or a detached
Ed25519 `source-export-attestation/v1` verified by configured destination trust.
Either proof SHALL bind the source logical vault identity, source installation
id/generation and active-fence digest, export operation identity, quiescence checkpoint,
archive SHA-256, manifest SHA-256, canonical source-census SHA-256, issued time,
expiry time, and signer key identifier. Caller-supplied verifier keys or expected
identity claims SHALL NOT create trust.
Hosted proofs SHALL additionally bind a typed routing `source_cell_id`; local
proofs SHALL omit it. Cell, vault, and installation identifiers SHALL be
validated in distinct namespaces and `source_cell_id == source_vault_id` SHALL
be malformed.

Ed25519 signed bytes SHALL be
`u32be(len("exomem.source-export-attestation/v1")) ||
"exomem.source-export-attestation/v1" || u64be(len(jcs_claim_bytes)) ||
jcs_claim_bytes`; the signature SHALL not occur in the claim object and the raw
64-byte signature SHALL use unpadded base64url. Source private keys SHALL remain
in cell/machine custody; no shared source HMAC secret SHALL be provisioned to the
destination. `SourceExportVerifierRecord/v1` SHALL bind the raw 32-byte Ed25519
public key in unpadded base64url and signer key id `ed25519-sha256:` plus
lowercase SHA-256 hex of those raw bytes, purpose=`vault-consolidation-export`, destination audience/trust
domain, exact allowed source vault/installation/generation and applicable typed
Hosted cell,
status, not-before/not-after, revocation, and registry generation. Records SHALL
live in private no-follow destination/control-plane state, never the archive,
run request, or recallable Knowledge Base. Verification
SHALL occur at intake, apply, retirement clearance, and retirement consumption.
Rotation MAY overlap two explicit public records, but revoked/premature/expired,
wrong-purpose/audience/source keys fail. Public-key/revocation history needed to
verify retained lineage SHALL survive retirement/evidence retention. RFC 8032
and Exomem canonical-claim fixed vectors SHALL be normative. A named
control-plane receipt alternative SHALL bind the same claims through an equally
closed issuer/purpose/audience/source/validity/revocation record.

The run SHALL derive the immutable source fingerprint as SHA-256 over the common
JCS frame with ASCII domain `exomem.consolidation-source-fingerprint/v1` and a
closed object of verified claims plus authentication-proof digest. It SHALL
derive the destination snapshot fingerprint with ASCII domain
`exomem.consolidation-destination-snapshot/v1` over a closed object containing
destination logical vault identity, installation id/generation and active-fence
digest, authenticated identity/root-binding fingerprint,
canonical census digest, active-policy fingerprint, access-state fingerprint,
and review-state fingerprint. The canonical destination census SHALL include all
canonical owned knowledge, append-only artifacts, authored history, access
metadata, review state, and active policy source; its exclusions SHALL be fixed
by schema to rebuildable derivatives, runtime logs/locks/caches, the entire
`_Consolidation/**` subtree, every receipt chain, all seal/journal/control state,
and private staging. The plan SHALL bind the active run's immutable
materialization control basis and semantic predecessor; later journals SHALL
bind their own current predecessor/control digests without requiring global
receipt-head or full mutable run-state equality. No current, older, or concurrent run/evidence
state SHALL enter a content preimage or rollback target. Neither inventory
operation SHALL mutate the source or destination corpus.

The canonical source content census SHALL use the same fixed exclusions for
every `_Consolidation/**` subtree, receipt chain, seal/journal/control state,
runtime/derived state, and private staging. Archive/manifest digests SHALL remain
separate attested claims, so exclusion from content census neither removes an
artifact-integrity check nor imports control/evidence state into rollback.

#### Scenario: Authenticated quiesced archive is admitted

- **WHEN** the archive and every manifest digest verify, its quiescence checkpoint is current, and its detached authentication proof verifies against destination-configured trust
- **THEN** the run records immutable source and destination snapshot fingerprints
- **AND** inventory begins without restoring into or mutating either active vault

#### Scenario: Self-consistent but unauthenticated archive is refused

- **WHEN** a caller presents an archive whose internal manifest is self-consistent but whose transport receipt or detached signature is missing, expired, untrusted, or bound to different claims
- **THEN** start fails with a stable content-free intake error
- **AND** no durable plan, candidate publication, or destination mutation occurs

#### Scenario: Source bytes change after attestation

- **WHEN** any archive byte, manifest entry, source census, source identity, or quiescence claim differs from the authenticated proof
- **THEN** the run refuses the artifact rather than recomputing authority from the changed archive
- **AND** a new authenticated export and new run are required

### Requirement: Local and Hosted cell identities are durable and non-caller-selected

Every consolidation-capable local or Hosted cell SHALL have a private no-follow
identity record outside `Knowledge Base/` containing an active logical
`vault_id`, an `installation_id`, and an authenticated root binding. `vault_id`
SHALL identify the logical corpus and `installation_id` SHALL identify its
serving instance. The record SHALL be authenticated by a cell- or machine-owned
key unavailable to ordinary commands and excluded from portability archives.
Neither identity, root binding, signer/verifier key, nor trust decision SHALL be
selectable from consolidation request content.

A Hosted `cell_id` SHALL remain a distinct typed routing/deployment identity,
not an alias for logical `vault_id` or serving `installation_id`. Identity,
gateway, owner-context, and export records SHALL carry and validate those fields
independently. Equality or namespace coercion between `cell_id` and `vault_id`
SHALL be rejected before readiness, owner-context construction, export, or
consolidation admission.

A legacy unbound vault SHALL become consolidation-capable only through a
one-time authenticated local-owner adoption under exclusive lifetime/writer
authority. Adoption SHALL generate both ids, bind the stable canonical
root/filesystem identity atomically, retain its census only as immutable adoption
provenance, and register installation ownership; it SHALL not accept ids from the
caller. Ordinary legitimate writes SHALL not require identity-record
reauthentication. An authenticated owner move/rebind of the same installation
SHALL preserve its logical and installation ids after proving the prior binding
and stable source root, and SHALL bind the new stable root/filesystem identity.
A copied/malformed identity record, missing machine key, installation id claimed
by another root/cell, or conflicting logical/root/fence binding SHALL fail closed
before export, intake, or run creation. Current content census SHALL instead be
computed after quiescence and bound by export/apply/retirement checkpoints.

A rehearsal clone SHALL be created by an explicit clone operation that generates
a new active `vault_id` and `installation_id` while recording immutable
`clone_of_vault_id`, `clone_of_installation_id`, and
`clone_of_snapshot_digest`. Copying the original identity record SHALL not create
a clone. A real cutover SHALL require source and destination logical vault ids
and installation ids to be distinct. A cloned rehearsal SHALL require distinct
active clone ids/installations and approved distinct source/destination clone-of
lineages. An offline failover restore that intentionally preserves a logical id
under the portability contract SHALL not be represented as a rehearsal clone.

Failover preserving a logical vault id SHALL be a fenced
`vault-identity-transfer/v1`, not a copied binding. Identity state SHALL contain
a monotonic installation generation and active-fence digest. The target SHALL
generate a fresh installation id/challenge; the authenticated transfer SHALL
bind logical vault, source installation/generation, target installation/challenge,
exact export/census/checkpoint, operation, validity, and target generation N+1.
An authoritative registry compare-and-swap SHALL fence/deactivate source N and
reserve target N+1 before target readiness, after which target activation
consumes the reservation and stale source admission refuses. Only exact
source-active, source-fenced/target-pending, or target-active recovery states
SHALL advance. Two active installations for one logical vault, skipped generation,
caller-selected target id, or unavailable fencing authority SHALL fail closed;
an offline candidate MAY remain unserved until fencing or adoption as a new
logical lineage.

Local export proofs SHALL be authenticated by the source cell/machine-owned key
or a trusted local control transport and verified only against configured trust.
Caller-supplied keys, copied identity state, or archive-contained trust claims
SHALL not authenticate a source.

#### Scenario: Legacy local vault is adopted

- **WHEN** an authenticated local owner adopts an unbound vault under exclusive authority
- **THEN** the engine generates and durably binds a new logical vault id and installation id without accepting either from input
- **AND** subsequent export attestations are authenticated by cell/machine-owned trust outside the archive

#### Scenario: Bound vault root moves on the same installation

- **WHEN** the owner-authorized rebind proves the prior authenticated identity record and stable source root at a new filesystem identity
- **THEN** the logical vault id and installation id are preserved while only the authenticated root binding changes
- **AND** an unproved copy or simultaneous second-root claim fails closed

#### Scenario: Adopted vault receives a legitimate write

- **WHEN** an adopted local vault changes canonical content normally and is later quiesced for export
- **THEN** its stable identity/root binding remains valid and export signs the newly computed current census
- **AND** no identity-record re-MAC or equality with the immutable adoption census is required

#### Scenario: Rehearsal clone is created

- **WHEN** the explicit clone operation copies an authenticated snapshot for rehearsal
- **THEN** it generates distinct active vault and installation ids and records immutable clone-of ids and snapshot digest
- **AND** the clone remains traceable to the real lineage without being mistaken for the same active cell

#### Scenario: Source and destination resolve to the same identity

- **WHEN** real-cutover admission resolves equal logical vault ids, equal installation ids, or a copied/colliding binding
- **THEN** start fails before inventory or resource allocation
- **AND** request-supplied replacement identities cannot bypass the collision

#### Scenario: Failover target preserves logical identity

- **WHEN** restore prepares a target intended to preserve the source logical vault id
- **THEN** target readiness requires a fresh installation id at generation N+1 and proof that authoritative compare-and-swap fenced source generation N
- **AND** the system never admits two active installations under that logical vault id

#### Scenario: Hosted cell id happens to equal vault id

- **WHEN** a Hosted binding, gateway assertion, or restored identity record supplies the same serialized identifier for `cell_id` and `vault_id`
- **THEN** typed identity validation refuses the binding before readiness or consolidation work
- **AND** neither routing identity nor caller content is coerced into logical vault authority

### Requirement: Plaintext staging is outside recall and run control state is structurally reserved

Each run SHALL have canonical durable control state at
`Knowledge Base/_Consolidation/runs/<run_id>/`. `_Consolidation` SHALL be a
structurally reserved administration subtree: generic file write, edit, move,
delete, adoption, restore, and ingestion operations SHALL refuse it; corpus and
index walkers SHALL exclude it; and normal content projectors SHALL NOT release
it. Detailed run state SHALL be readable only through an owner-authorized
consolidation control action.

Extracted source bytes, candidate bytes, and destination rollback preimages
SHALL reside in durable private artifact storage outside `Knowledge Base/`, the
source archive, and every recall/index walk. Canonical run state and receipts
SHALL store only opaque artifact references, algorithms, hashes, byte counts,
bounded counts, and lifecycle facts, never an absolute private-storage path or a
private body. Missing, changed, or hash-mismatched required artifacts SHALL
block apply, rollback, unseal, and source-retirement clearance as applicable.

#### Scenario: Broad recall runs during planning

- **WHEN** any ordinary principal, including the owner through a normal content command, searches or browses the entire destination while a consolidation run contains extracted private source bodies
- **THEN** neither `_Consolidation` run state nor private staging is indexed, projected, counted, or returned
- **AND** only the owner-authorized consolidation control command can return bounded run details

#### Scenario: Private artifact is lost

- **WHEN** a stored artifact reference no longer resolves to bytes matching its recorded content hash
- **THEN** the next phase that requires the artifact refuses with an owner-visible recovery condition
- **AND** the system does not substitute archive, destination, or caller-provided bytes silently

### Requirement: Reconciliation classifies every object exhaustively before planning

The system SHALL build deterministic indexes over exact bytes, normalized paths,
durable identities, logical content, attachments, semantic anchors, references,
supersession/history edges, typed relations, Record items, media sidecars, and
policy/control state. Every source object SHALL receive exactly one primary
class under the following precedence, with dependency findings retained on its
row:

1. `C8 authority/control state`: policy, audience, grant, token, authorization
   session, receipt, run state, runtime binding, access-control, or review
   authority that cannot become live destination authority by copying;
2. `C6 divergent identity collision`: one durable identity names different
   exact object bundles;
3. `C5 divergent path collision`: one normalized destination path names
   different exact bytes and no C6 collision applies;
4. `C4 content-equivalent identity divergence`: deterministic logical content,
   excluding only the governed identity field, is equal while durable
   identities differ or are missing;
5. `C3 identity-equivalent relocation`: durable identity and exact object bytes
   match while paths differ;
6. `C1 exact duplicate`: normalized path and exact object bytes match;
7. `C7 dependent structural conflict`: no direct path/identity class applies,
   but a semantic-unit anchor, link/reference, history/supersession edge, typed
   relation, media/sidecar pair, Record identity, lifecycle/type rule, or
   append-only invariant would be ambiguous or invalid under the tentative map;
8. `C2 unique addition`: none of C1 or C3-C8 applies.

The deterministic defaults SHALL make C1 a content-publication no-op, make C2 a
planned addition at its collision-free destination path, and make C3 reuse the
destination identity while deterministically rewriting planned references
through the identity/path map without publishing a duplicate. Every C1 row SHALL
still persist an owner-only provenance mapping from authenticated source
snapshot/object/path/identity/hash to exact destination
object/path/identity/hash; the mapping SHALL contribute to the reconciliation
and plan digests and its mapping-set digest SHALL be retained in plaintext-free
evidence. C4-C8 SHALL remain unresolved until an
owner selects a schema-enumerated resolution for every conflict and dependent
finding. C8 source authority SHALL be retained only in the authenticated source
artifact/provenance and SHALL NOT be installed as destination authority.

The plan SHALL preserve Sources, Evidence, Records, media and sidecars,
semantic-unit anchors, stable identities, authored history, relations,
citations, review state, and provenance. Append-only Sources and Evidence SHALL
be deduplicated only on exact bytes, retained at a collision-free path, or kept
in the source artifact; they SHALL NOT be overwritten or body-rewritten.
Derived indexes, embeddings, and caches SHALL be rebuilt, not copied. No object,
edge, sidecar, or conflict SHALL be silently dropped.

#### Scenario: Identity and path both collide

- **WHEN** a source object shares a durable identity and normalized path with a destination object but their exact object bundles differ
- **THEN** precedence assigns C6 rather than C5 or a lower class
- **AND** planning remains blocked until the owner resolves the identity collision and every affected dependency

#### Scenario: Logical duplicate has different governed identity

- **WHEN** source and destination logical content is equal after removing only the governed identity field but their durable identities differ
- **THEN** the object is classified C4 and is not exact-byte deduplicated
- **AND** the preview shows the allowed identity/reconciliation choices and their downstream reference effects

#### Scenario: Exact duplicate publishes no second object

- **WHEN** a source object is classified C1
- **THEN** its content action is a no-op but its exact source-to-destination provenance mapping remains durable in owner-only run state and bound by the plan/receipt digests
- **AND** later archive disposition does not erase the recorded fact that both authenticated inventories mapped to the destination object

#### Scenario: An apparently unique object has an invalid dependency

- **WHEN** a source object's path and identity are unique but its planned relation, semantic anchor, media pair, Record item, or lifecycle dependency would become ambiguous or invalid
- **THEN** it is classified C7 rather than C2
- **AND** no complete plan is issued until the dependent conflict is resolved

### Requirement: Destination policy uses freshly attested destination principals

Source policy documents, audience identifiers, grants, credentials, tokens,
authorization sessions, release approvals, and runtime bindings SHALL be review
inputs only and SHALL NOT be copied into live destination authority. Every
principal named by the prospective destination policy or representative
disclosure matrix SHALL have a fresh
`destination-principal-attestation/v1` issued by a trusted destination surface.
The attestation SHALL bind the destination vault identity, issuer/surface,
resolved canonical principal, allowed purposes, issued time, expiry time,
authentication/session binding, nonce, and attestation fingerprint. The trusted
surface SHALL resolve the canonical principal; request content SHALL NOT select
or override it.

The consolidation plan SHALL emit newly authored destination scope, rule,
bridge, and exact-release approval documents through the existing governance
grammar and prospective compiler. Existing per-scope grant, deny dominance,
bridge, exact-release, authorization-session, and release-gate semantics SHALL
apply unchanged. Approval and apply SHALL refuse any missing, expired,
destination-mismatched, session-mismatched, or source-copied principal identity.

#### Scenario: Source audience identifier is proposed for reuse

- **WHEN** a proposed policy or disclosure row contains an audience id, grant, token, or session copied from the source
- **THEN** prospective planning refuses it as non-portable authority
- **AND** the caller must supply a fresh attestation from the destination surface for the intended principal and purpose

#### Scenario: Destination attestation expires before apply

- **WHEN** all conflicts were reviewed but a bound principal attestation is expired or no longer matches its authenticated destination session at apply admission
- **THEN** apply refuses before sealing or publication
- **AND** a refreshed attestation changes the plan preimage and requires a new owner confirmation

### Requirement: One exact review binds content, policy, principals, verification, and rollback

`plan` SHALL persist canonical JSON with schema
`exomem.consolidation-plan/v1`. Its preimage SHALL contain:

- schema version, protocol version, run id, and run mode;
- immutable source and destination snapshot fingerprints and the expected
  destination-preimage census digest;
- sorted source-inventory, reconciliation, conflict-decision, identity-map,
  path-map, and dependency-map digests;
- every exact content action with expected-before and planned-after hashes, plus
  the deterministic journal-batch partition digest;
- every exact canonical prospective policy document and its compiled policy,
  bridge, and exact-release approval fingerprints;
- the fresh destination owner/principal attestation-set digest;
- the representative principal-by-purpose-by-item disclosure-matrix digest;
- positive/negative verification-plan and nonterminal apply rollback-contingency
  digests, plus required source-retention state through cutover and its approved
  recovery window; this SHALL NOT pre-authorize or predict a future terminal-run
  rollback plan;
- immutable `control_basis_digest` and closed
  `plan_successor_automaton_digest`, bound separately from the excluded content
  census and not defined as the current mutable run state/global receipt head;
- the server-derived complete impact summary and trusted ordered-section/page
  rendering definition, including every create, overwrite, removal, relocation,
  C1/provenance mapping, policy/principal/disclosure change, batch, rollback/
  no-loss consequence, and unresolved count; and
- plan creation time, validity deadline, and fresh plan nonce.

All source-export claim, cutover, rollback, retirement, rendering, event, and fingerprint preimages
SHALL use RFC 8785 JCS over a closed value subset: NFC-valid strings; duplicate
keys, including post-NFC duplicates, refused; JCS escaping/property ordering;
UTC RFC3339 millisecond timestamp strings; bounded non-negative integer
counts/ordinals/generations/byte sizes/TTL values no greater than 2^53-1; and no
other numbers, negative zero, float/exponent form, NaN, infinity, or unspecified
null. Hash input SHALL be `u32be(domain ASCII length) || domain ASCII ||
u64be(JCS byte length) || JCS bytes`. `plan_digest` SHALL be SHA-256 over that
framing with domain `exomem.consolidation-plan/v1` and SHALL not occur inside its
own preimage. Cross-runtime fixed vectors SHALL pin NFC, escaping, order,
integer boundaries, framing, and digest.

`control_basis_digest` SHALL be framed-JCS SHA-256 under exact domain
`exomem.consolidation-control-basis/v1` over a closed object containing run,
plan kind/materialization operation, basis run revision, immutable plan-input
set, fresh plan nonce, the committed same-run predecessor event id/nested
payload digest resolved from the owner-only materialize successor context, and
`plan_successor_automaton_digest`. It SHALL exclude `plan_digest`,
mutable rendering/approval/token rows, the complete reserved run subtree, and
current receipt head.

The framed-JCS automaton object SHALL have exactly `schema`, `initial_state`,
`states`, `terminal_state`, `minimum_pages`, `page_count_source`, `transitions`,
`retry_rule`, and `unexpected_event_rule`. Its fixed values SHALL be schema and
domain `exomem.consolidation-plan-successor-automaton/v1`, initial
`plan-materialized`, ordered states `[plan-materialized,render-begin,
render-page,render-ack,render-complete,approval,token-reservation]`, terminal
`token-reservation`, minimum pages `1`, and page-count source
`stored-plan-rendering-definition`. `transitions` SHALL be an ordered seven-row
array whose objects have exactly `ordinal`, `from_state`, `to_state`, `guard`:
`(0,plan-materialized,render-begin,once)`,
`(1,render-begin,render-page,page-0)`,
`(2,render-page,render-ack,same-page)`,
`(3,render-ack,render-page,next-page-if-any)`,
`(4,render-ack,render-complete,last-page)`,
`(5,render-complete,approval,complete-coverage)`, and
`(6,approval,token-reservation,matching-kind-token)`. `retry_rule` SHALL be
`adopt-existing-identical-event` and `unexpected_event_rule` SHALL be
`stale-plan`. The digest is outside the object. The object contains no run,
plan, page, control-basis, or self digest; the plan separately binds the exact
ordered rendering definition/page count.

For that page count, every event SHALL declare the immediately preceding
committed event id/payload digest, same run/kind/plan, and operation id through
the exact owner-only successor context returned by the prior logical terminal.
Identical retry SHALL adopt its deterministic event without a new successor.
Skipped/reordered/duplicated new transitions, another render session,
reconcile/new-plan, or any unexpected same-run control/evidence event SHALL
stale the plan. Status emits no event and owner-detail returns only the current
opaque context ref/digest without minting it; unrelated-run events MAY
interleave only in the physical receipt chain.

At apply, the request SHALL resolve the approval terminal's exact
`successor_context_ref`/digest and revalidate its protected predecessor and
approval-token facts, then the JTI reservation SHALL create the one permitted
token-reservation event.
The apply journal SHALL separately bind that committed terminal as
`apply_predecessor_event_id`/`apply_predecessor_digest` and revalidate it before
seal intent/recovery continuation. It SHALL NOT compare the current global
receipt head or whole mutable run-control state to the materialization-time
plan preimage. Terminal rollback and retirement clearance SHALL use the same
kind-bound ancestry rule.

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

Only a product terminal in the exact table SHALL return the plan-entry context;
its `eligible_plan_kinds` SHALL be precisely the table rows valid for the same
current terminal and facts. After materialization, the prior logical terminal
SHALL durably return the exact context needed for each protected successor:
materialization to render begin; each render begin/page/acknowledgement to its
one next page, acknowledgement, or completion; completion to approval; and
approval to cutover apply, terminal-plan rollback, or retirement clearance by
plan kind. Any nonterminal apply/recover/verification result that leaves sealed
contingency rollback eligible SHALL instead return a
`rollback-nonterminal-contingency` context. That second kind resolves
server-side to the original apply journal, contingency/publication digests,
current predecessor, purpose-bound authority ref/digest, and deadline; none of
those protected values is accepted from the rollback body or exposed in
status/terminal output. Owner-detail status
may return the already-durable current pair without minting or advancing it.

#### Scenario: Every plan kind starts from one table row

- **WHEN** materialization resolves a successor context produced by each allowed table terminal, including repaired targets, rollback-complete, retirement-pending-forward-only, and retirement-finalize
- **THEN** its plan intent uses that exact committed context predecessor and the product terminal exposes exactly the applicable eligible-kind set
- **AND** an unlisted phase, product terminal, repair target, run mode, or eligibility state returns no pair and cannot start a plan

`plan(operation="render")` SHALL load the exact stored plan by run id, kind, and
digest and SHALL derive the impact summary, ordered sections, pagination, page
digests, and total rows/pages server-side. It SHALL never render caller/agent
plan text. A trusted owner surface SHALL bind each served/acknowledged page to
one authenticated owner/session/surface. Only complete ordered coverage SHALL
mint `plan-rendering-completeness/v1`, binding plan kind/digest, all section/page
digests and totals, impact-summary digest, owner/session/issuer, issued/expiry,
and nonce. This proves complete trusted presentation/acknowledgement, not
subjective comprehension.

`approve` SHALL reload the stored bytes and require the exact expected plan
digest plus that unexpired rendering-completeness capability. An agent-supplied
display/digest, skipped/truncated page, copied preview, or `approved=true` SHALL
not count. The single-use kind-bound token SHALL contain only version, plan kind,
run id, plan digest, rendering-completeness digest, JTI, expiry, and
authentication. The matching executor SHALL reserve the JTI for one operation
journal; only an identical retry may resume that operation. Any source
or destination drift, changed decision, principal attestation, policy,
verification plan, rollback plan, expiry, token, or apply argument SHALL require
a new plan and confirmation.

#### Scenario: Owner confirms the exact joint plan

- **WHEN** a trusted surface has loaded the exact stored plan, served and recorded acknowledgement of every server-defined section/page plus impact summary, and the authenticated owner confirms its digest before expiry
- **THEN** the system issues a single-use token bound to the complete content-plus-policy preimage
- **AND** changing even one content action, policy byte, conflict decision, principal attestation, probe, or rollback term invalidates that token

#### Scenario: Agent supplies a digest without complete rendering

- **WHEN** an agent or caller presents the right plan digest but the trusted rendering record is absent, truncated, cross-session, or lacks one required page
- **THEN** approval refuses without minting a token
- **AND** caller display text or a claimed completeness boolean cannot fill the gap

#### Scenario: Caller submits an approval boolean

- **WHEN** MCP, REST, CLI input, or an agent supplies `approved=true` without the surface-injected trusted owner-confirmation capability
- **THEN** no approval token is issued
- **AND** the response does not imply that the caller-controlled field conveyed owner authority

#### Scenario: Approval acknowledgement is retried

- **WHEN** acknowledgement is lost after a token JTI has been reserved and the caller retries the identical mutation identity and payload
- **THEN** the existing operation and terminal state are returned or resumed
- **AND** the JTI cannot authorize a second operation or changed payload

#### Scenario: Review events are valid causal successors

- **WHEN** materialization, render begin, every ordered page/ack, render complete, approval, token reservation, and apply start each declare the exact preceding committed same-run event id/payload digest
- **THEN** the immutable control basis remains an ancestor while apply binds and revalidates its current token-reservation predecessor separately
- **AND** interleaved receipts for another run may change physical `prev` without staling this plan

#### Scenario: Same-run control event is unexpected

- **WHEN** review skips or reorders a page, adds a duplicate semantic transition, names the wrong predecessor, or another reconcile/new-plan/control event intervenes
- **THEN** approval/token reservation/apply refuses the stale successor chain
- **AND** neither current global receipt-head equality nor an identical retry that adopts its existing event is confused with that same-run staleness

### Requirement: Apply is an owner-inclusive sealed, policy-first journaled saga

Apply SHALL use the ordered durable state machine `approved -> sealing -> sealed
-> preimage-ready -> policy-active -> publishing -> rebuilding -> verifying ->
verified -> transport-stopping -> transport-verifying -> transport-verified ->
routing-opening -> complete`. Before publication it SHALL verify the immutable
control basis and exact allowed successor chain, reserve the approval JTI and
operation id, persist/revalidate the separate apply-predecessor terminal, acquire exclusive writer/lifecycle authority,
persist and drain a destination-wide seal, and revalidate source artifacts,
both snapshots, principal attestations, conflict completeness, and the entire
plan preimage.

While sealed, every normal content read and mutation surface SHALL return the
same content-free sealed outcome for every ordinary principal, including the
owner. There SHALL be no normal-owner exception. Only an unforgeable in-process
`ConsolidationAuthority`, bound to destination vault, run, operation journal,
phase, and allowed action, MAY inspect control state, access private artifacts,
publish the exact plan, restore the preimage, or invoke named probes. It SHALL
not be serializable, persisted as a reusable credential, or accepted from any
request field.

After proving a complete content-addressed destination preimage, apply SHALL
activate the exact approved restrictive policy through the existing governance
journal/marker/critical-receipt protocol. Only then SHALL it publish exact
content actions in bounded deterministic `batch_atomic_write` batches. Each
batch SHALL record domain-separated prior, prepared, and final fingerprints,
receipt-first intent, and a durable run-journal transition. The first committed
content batch SHALL be the publication boundary. Lexical, embedding,
semantic-unit, graph, media, freshness, identity, and review derivatives SHALL
then be rebuilt solely from canonical destination bytes. After in-process
verification, public routing SHALL be durably stopped/drained and the exact
destination post-cutover census, release/build, selected surface
profile/descriptor, configuration/trust/principal mapping, and routing proof
SHALL be bound. Only then MAY trusted control temporarily remove/bypass this
consolidation seal for normal-auth black-box MCP/REST/Hosted/CLI probes while
public ingress remains stopped. No internal authority or special principal MAY
cross those transports. Bound transport success permits routing open/complete;
failure/restart keeps routing stopped and deterministically re-seals or enters
owner recovery/rollback. Clone results SHALL not replace exact real-cell
transport verification.

#### Scenario: Owner reads during publication

- **WHEN** the destination is sealed and its owner invokes an ordinary ask, read, browse, media, review, graph, history, export, or file command
- **THEN** the command returns the stable content-free sealed outcome without run id, phase, counts, item existence, or recovery state
- **AND** no partial policy/content state is observable

#### Scenario: First content batch is about to publish

- **WHEN** the exact preimage is verified but the approved restrictive destination policy has not reached its durable active terminal
- **THEN** no content batch is admitted
- **AND** the destination remains sealed for deterministic abort or recovery

#### Scenario: All batches publish successfully

- **WHEN** restrictive policy is active and every approved content batch reaches its exact final fingerprint
- **THEN** the system rebuilds every derived store from canonical destination bytes
- **AND** it remains sealed until verification and terminal evidence complete

#### Scenario: Exact-cell transport verification fails

- **WHEN** a normal-auth black-box probe fails or the process restarts while public routing is stopped in `transport-verifying`
- **THEN** public routing never opens and the exact destination is re-sealed or retained in owner-only recovery
- **AND** the approved rollback remains reachable without serializing internal authority

### Requirement: Recovery classifies exact durable state without semantic replay

The seal marker and current consolidation phase SHALL load before ordinary
command admission after every process start. Missing, malformed, conflicting,
or journal-inconsistent nonterminal seal state SHALL fail closed. Every phase,
policy operation, and content batch SHALL have exact prior, prepared, and final
fingerprints. Startup, `status`, and `recover` SHALL classify the current seal,
policy, canonical files, artifacts, derived readiness, and receipt terminals
against those values.

Exact prior state MAY abort; exact prepared state MAY finish missing evidence
and activate; exact final state MAY advance. Mixed or third state SHALL remain
sealed and expose bounded repair facts only to the owner control path. Recovery
SHALL NOT ask an agent to re-resolve conflicts, re-author policy, or regenerate a
plan, and SHALL NOT invoke a content mutation twice. Retry with changed
arguments, artifact references, attestations, plan/token, destination identity,
or run mode SHALL conflict with the recorded operation.

#### Scenario: Process crashes after prepared bytes are durable

- **WHEN** restart observes exactly the recorded prepared fingerprint but the phase terminal or critical receipt was not durably completed
- **THEN** recovery completes the missing evidence/activation transition idempotently
- **AND** it does not re-run conflict reasoning or apply the mutation a second time

#### Scenario: Files match neither prior nor final fingerprint

- **WHEN** recovery observes a mixed or third canonical state for any protected phase or batch
- **THEN** the destination stays sealed and automatic advance/abort is refused
- **AND** only the owner control surface receives bounded repair diagnostics

### Requirement: Verification proves allowed access and non-disclosure before unseal

The approved verification plan SHALL include positive owner access, positive
delegated access for every allowed representative principal/purpose/domain,
positive access only to explicitly approved compiled abstractions, and negative
tests for private bodies, source-only provenance, denied domains, cross-purpose
reuse, stale authorization, enumeration, counts, snippets, links, media,
history, errors, and timing differentials. Coverage SHALL exercise every
relevant MCP, REST, CLI, Hosted, search, read, browse, graph, review, media,
export, resource, and error path through the real command adapters.

The narrow probe capability MAY bypass only the outer destination seal and SHALL
remain an in-process object. Pre-unseal probes SHALL call the same
adapter/serializer functions internally with ordinary freshly attested
representative authorization contexts and SHALL still use the identity resolver,
authorization-session binding, release decision, projector, scrubber, response
adapter, and receipt collector. The capability SHALL never be serialized into,
accepted from, or transmitted through MCP, REST, CLI, Hosted, retry, or other
black-box requests. Probe input/output persisted outside owner-only run state
SHALL remain content-free. Any missing coverage, positive mismatch, negative
leak, projector error, receipt failure, or integrity failure SHALL keep the
destination sealed.

Supplemental transport-level MCP, REST, CLI, and Hosted parity SHALL be proven on
disposable or cloned cells after an equivalent seal/unseal cycle using only
normal surface authentication. It SHALL not claim that an internal authority
crossed the transport and SHALL not replace either mandatory sealed in-process
verification or the real cutover's exact-destination transport gate. For a real
cutover, after in-process verification and while public routing remains durably
stopped/drained, trusted control SHALL temporarily remove/bypass only the exact
operation's consolidation seal and exercise normal-auth black-box MCP, REST,
Hosted, and CLI paths against that exact post-cutover census. Bound transport
success and basis revalidation SHALL precede routing open; failure/restart SHALL
re-seal or enter owner-only recovery and SHALL never open traffic.

#### Scenario: Delegated positive and negative probes pass

- **WHEN** every allowed representative request returns exactly the approved projection and every denied request is indistinguishable from absent content across all covered surfaces
- **THEN** the run may enter `verified`
- **AND** unseal remains blocked until the verified census and terminal evidence are durable

#### Scenario: Probe capability reaches the adapter

- **WHEN** a named verification probe is invoked while sealed
- **THEN** only an in-process phase-bound authority admits that internal call to the ordinary adapter/serializer pipeline
- **AND** a governance denial, projector failure, or scrubber failure is observed as a failed probe rather than bypassed

#### Scenario: Transport parity is exercised

- **WHEN** a disposable or cloned destination has completed the seal/unseal lifecycle and black-box MCP, REST, CLI, or Hosted verification runs
- **THEN** each request uses only its normal authenticated principal/session and traverses the real transport
- **AND** no internal consolidation authority is serialized, injected, or inferred from the request

#### Scenario: Real destination transport proof is interrupted

- **WHEN** the exact real destination is being exercised through normal-auth transports with routing stopped and a probe, bound basis, or process fails
- **THEN** no routing-open terminal is authorized and startup deterministically re-establishes the consolidation seal or owner-only recovery
- **AND** disposable/clone evidence cannot substitute for the missing exact-cell terminal

### Requirement: Every explicit terminal-run rollback is separately planned and approved

Recovery of a nonterminal apply MAY execute only the rollback contingency already
bound by its cutover plan. Every owner-requested rollback after the apply
operation reaches a terminal SHALL first use
`plan(plan_kind="rollback", operation="materialize")` to persist
`exomem.consolidation-rollback-plan/v1`; `rollback` SHALL only execute a separately
rendered/approved plan and SHALL NOT infer a hidden target or treatment.

The rollback request SHALL be a closed `oneOf` selected by
`rollback_mode=nonterminal-contingency|terminal-plan`.
Both branches SHALL require the current `successor_context_ref` and digest.
`nonterminal-contingency` SHALL resolve that context server-side to the original
apply operation/journal, cutover-plan and approved contingency digests, current
publication-state digest and control predecessor id/digest, and the owner-only
durable contingency-authority ref/digest reserved by that original apply; every
one of those request-body fields SHALL be forbidden. It SHALL require that
apply remain sealed/nonterminal inside its
approved recovery window, forbid rollback-plan/token fields, and consume no new
rollback token. `terminal-plan` SHALL require the separately rendered/approved
rollback plan/token, resolve its approval predecessor from the matching
successor context, and SHALL forbid every
original-apply/contingency field. The contingency authority SHALL be bound to
run/apply/plan/contingency/window, usable once under current owner control, and
shall not serialize as `ConsolidationAuthority`. Both branches SHALL reserve
operation idempotency and return their own exact terminal branch; identical
retry reuses it, while changed mode/revision/context-resolved predecessor/
journal/authority/input conflicts before restore.

The canonical rollback preimage SHALL bind schema/protocol, run id and
plan-materialization operation id, target (`pre-cutover` or named
`post-cutover-forward`), creation/
deadline/nonce; original cutover plan/terminal; source/destination vault,
installation/generation, identity binding and snapshots; exact current,
pre-cutover, post-cutover, and target census/manifest/policy/access/review
fingerprints; source/archive retention and retirement/finalization state;
post-retirement rollback mode and forward-snapshot proofs; and the sorted union
of every current object, target object, imported object/C1 mapping, and every
post-cutover create/modify/move/delete.

Each union row SHALL have exact origin/current/target hashes, dependencies,
conflicts, before/after hashes, surviving-copy proof, and one explicit treatment:
restore target, retain current at an exact collision-free path, reapply current
after target, retain both at exact paths, or owner-confirmed discard. The plan
SHALL also bind target policy/access/review documents, deterministic batches,
derived rebuild, positive/negative verification, receipt plan,
rollback-of-rollback recovery artifact, impact summary, and trusted rendering
definition. Every imported object remains represented even if the target removes
it; every later write remains represented even if unrelated. No implicit/default
treatment or unresolved row is permitted.

The rollback digest SHALL be SHA-256 over the common JCS frame with exact ASCII
domain `exomem.consolidation-rollback-plan/v1` and SHALL remain outside the bytes.
Complete trusted rendering plus `approve(plan_kind="rollback")` SHALL issue a
single-use rollback-kind JTI. Before reserving it, execution SHALL revalidate the
exact current census, retirement/copy proofs, target, and token, then follow
`rollback-approved -> rollback-sealing -> rollback-revalidating ->
rollback-restoring -> rollback-rebuilding -> rollback-verifying ->
rollback-complete`. Each phase SHALL be receipt-first and idempotently classified;
identical operation/request resumes, while changed target, plan/token, census,
treatment, copy proof, identity, or owner conflicts.

#### Scenario: Later write receives an explicit treatment

- **WHEN** the destination changed after cutover and a rollback plan is materialized
- **THEN** every later create, modify, move, and delete appears in the union inventory with one exact treatment and impact
- **AND** `rollback` without that separately rendered/approved plan returns `ROLLBACK_RECONCILIATION_REQUIRED` and writes nothing

#### Scenario: Imported object would lose its final copy

- **WHEN** a pre-cutover target removes an imported object and neither source, authenticated archive, destination treatment, nor retained forward snapshot will preserve its exact bytes/provenance
- **THEN** rollback planning refuses before approval
- **AND** no executor can silently default the row to removal

#### Scenario: Lost acknowledgement retries rollback across surfaces

- **WHEN** a rollback effect commits but acknowledgement is lost and the same owner retries the identical operation id and tagged request on another surface
- **THEN** initial delivery is `{"success":true,"data":{"delivery":"initial","terminal":T}}` and retry is `{"success":true,"data":{"delivery":"replayed","terminal":T}}`, with byte-identical canonical `T`, `outcome=committed`, and no repeated effect
- **AND** an intentional identical later rollback requires a new operation id

#### Scenario: Nonterminal contingency is explicitly selected

- **WHEN** rollback selects the nonterminal branch with the opaque current successor context resolving to the exact still-sealed apply journal, publication state, cutover contingency, predecessor, and reserved authority
- **THEN** it reaches the reviewed contingency without requiring or accepting a terminal rollback plan/token
- **AND** terminal apply state, mixed or caller-supplied protected fields, stale context/authority, or a changed predecessor refuses before restore

### Requirement: Abort, rollback, and source retirement have distinct safe meanings

Before policy activation, apply SHALL verify a full content-addressed destination
preimage in private artifact storage, including policy, knowledge, append-only
artifacts, history, access/review state, and canonical metadata. Storage
admission SHALL fail before policy/content publication when that complete
preimage cannot be proven.

`abort` SHALL be available only before the first content batch commits. It SHALL
remove private candidate staging, restore any activated policy from the exact
preimage, close the token/journal as aborted, and unseal only after the canonical
destination census equals the approved prior snapshot. `rollback` SHALL be used
after the publication boundary; a nonterminal apply SHALL use only its approved
contingency, and a terminal run SHALL require the exact rollback plan above. It
SHALL restore the named complete target, apply every reviewed union-row
treatment, rebuild derivatives, and verify the target census before unseal.

If a terminal cutover has no approved rollback plan, or its current census has
drifted from that plan, rollback SHALL produce
`ROLLBACK_RECONCILIATION_REQUIRED` and require a new exact plan; it SHALL NOT
overwrite or discard later work.
Abort and rollback SHALL never rewind or delete append-only governance,
consolidation, or mutation receipts. Receipt churn is outside the canonical
snapshot census, and each action SHALL append its own intent, phases, and
terminal evidence.

#### Scenario: Abort occurs before publication boundary

- **WHEN** an approved run fails or is cancelled before its first content batch commits and the prior snapshot can be proven
- **THEN** abort restores any policy change, closes the run with append-only aborted evidence, and unseals the exact prior destination
- **AND** the source remains unchanged

#### Scenario: Implicit rollback is requested after a later destination write

- **WHEN** a completed cutover was unsealed and the current canonical census differs from the recorded post-cutover census
- **THEN** any implicit or stale-plan rollback refuses with `ROLLBACK_RECONCILIATION_REQUIRED`
- **AND** no later canonical write is overwritten until a new exact drift reconciliation is owner-reviewed and confirmed

#### Scenario: Rollback restores the approved target

- **WHEN** rollback is admitted under a nonterminal contingency or exact separately approved rollback plan
- **THEN** it restores and verifies the complete named target plus every reviewed retained/reapplied item and rebuilds derivatives before unseal
- **AND** cutover and rollback receipt history remains append-only and auditable

### Requirement: Rehearsal, real cutover, and source retirement require separate authority

A `cloned-rehearsal` run SHALL be bound to explicit source/destination clone
identities and SHALL exercise apply, the approved positive/negative matrix,
required crash/retry seams, and full rollback. Rehearsal proof MAY satisfy a
prerequisite of a later plan but SHALL NOT transfer its fingerprints,
attestations, plan digest, approval token, JTI, or consolidation authority.

A `real-cutover` SHALL require fresh source and destination fingerprints, fresh
destination principal attestations, a fresh exact joint plan, and a separate
owner confirmation. `retire-source` SHALL require a third, retirement-specific
owner confirmation after successful real cutover, current destination
verification, verified rollback-preimage availability, unchanged authenticated
source snapshot/quiescence checkpoint, and successful cloned-rehearsal proof
including rollback. Its exact retirement preimage SHALL declare one source
archive disposition: retain the verified opaque artifact under stated retention
terms, transfer it to a named trusted custodian with an authenticated custody
receipt, or authorize external
destruction after clearance. The canonical retirement preimage SHALL bind its
schema/version and nonce/deadline; run and real-cutover plan/terminal digests;
source/destination vault, installation, snapshot, and current-census
fingerprints; rehearsal/rollback proof digests; destination verification and
rollback-preimage artifact digests/readiness; unchanged source checkpoint;
exact archive disposition/terms; and the retained-versus-irrecoverable
provenance-statement digest. The schema and exact ASCII digest domain SHALL be
`exomem.consolidation-retirement-plan/v1`; its retirement digest SHALL be
SHA-256 over the common frame outside the canonical preimage bytes and SHALL be the value bound by the third human
confirmation. It SHALL also bind exactly one post-retirement rollback mode:

A transfer-to-custodian disposition SHALL bind the protected reference and
digest of one detached `archive-custody-receipt/v1`. Its closed signed claims
SHALL require the custodian identity/retention-domain digests, source
vault/installation/generation digests, exact archive/manifest/source-census
digests, transfer operation id, retention-terms digest, accepted/issued/
not-before/expiry timestamps, nonce, and signer key id. The signature SHALL be
raw 64-byte Ed25519 in unpadded base64url over the common framed JCS bytes with
exact ASCII domain `exomem.archive-custody-receipt/v1` and SHALL remain outside
the claim object; `not_before <= accepted_at <= issued_at < expires_at` SHALL
hold. Private `ArchiveCustodianVerifierRecord/v1` SHALL use the exact closed
field set defined by the portability contract and bind raw
32-byte Ed25519 public key, derived `ed25519-sha256:` key id, purpose
`vault-consolidation-archive-custody`, exact custodian/retention domain, allowed
source lineage and destination trust audience, status, validity, revocation,
and registry generation with bounded two-key overlap and retained trust history.

Retirement-plan materialization, clearance, source consumption, and every
rollback-plan materialization and rollback commit that counts the custodian
copy SHALL independently revalidate the exact signature, current verifier
status/validity/revocation, archive availability/digests, source census,
transfer operation, and unchanged retention terms. Cached or earlier success
SHALL not survive expiry, revocation, changed terms, artifact loss, wrong
domain/audience/lineage, or missing trust history; the copy is removed from the
surviving-copy ledger and the gate fails before effect when no verified survivor
remains.

- `pre-cutover-reversible`, requiring the authenticated source vault/archive,
  verifier/revocation history, and retention proof to survive the rollback
  window; or
- `forward-only`, required before source/archive bytes may become irrecoverable
  and requiring a separately retained verified
  `post-cutover-forward-snapshot/v1` containing every destination/imported byte,
  C1 mapping, identity/relation/history/citation/review/provenance record,
  policy/access state, and verification trust evidence.

Finalizing forward-only retirement SHALL permanently prohibit a pre-cutover
rollback target; later rollback may target only the verified forward snapshot or
a newly reviewed state preserving every imported bundle. At clearance,
consumption, completion, rollback planning, and rollback commit, a surviving-copy
ledger SHALL prove each imported byte/provenance bundle retains at least one
verified source, archive, destination, or forward-snapshot copy after the effect.
Zero surviving copies SHALL block before any destructive effect; a pre-cutover
preimage does not count because it predates imported bytes.

The ledger schema and exact ASCII digest domain SHALL be
`exomem.consolidation-surviving-copy-ledger/v1`. Its closed JCS bytes SHALL bind
run, governing retirement/rollback plan, proposed effect, current source/
destination/archive/forward-snapshot census and proof digests, and sorted rows
containing imported `bundle_digest`, candidate survivor kind/proof digest,
disposition, and bounded post-effect survivor count. The framed SHA-256 digest
SHALL remain outside the bytes. Missing rows, unverified candidates, or zero
counts SHALL invalidate it; receipts SHALL retain only digest and bounded counts.

Before issuing a forward-only clearance, the destination SHALL persist
`retirement-pending-forward-only/v1` bound to lifecycle ref, forward snapshot,
ledger, destination census, source/archive proof, and deadline. This pending
rollback fence SHALL refuse pre-cutover rollback planning/execution before the
source can consume clearance and SHALL survive restart or lost completion.
`recover` MAY remove it after expiry only when authenticated source-lifecycle
evidence proves the JTI was never consumed and the unchanged source/archive still
exists; uncertainty keeps it. Finalization SHALL convert the pending fence into
the permanent prohibition, not first create the prohibition after destruction.

The owner-visible preview SHALL state that if the
external archive is destroyed, destination canonical bytes, durable owner-only
source-to-destination/C1 mappings and identity/snapshot/attestation digests, and
plaintext-free receipts remain, while source-only bytes/control state and
archive-only provenance are no longer reconstructable. The command SHALL issue
only content-free clearance facts;
actual connector/routing shutdown, retention, backup/key destruction,
billing/account changes, filesystem deletion, or source disposal remain actions
of the separately authorized source/control-plane operator.

Clearance SHALL be a single-use purpose-bound source-lifecycle capability, not a
reusable report. It SHALL bind retirement/destination no-loss/verification proof
digests, source checkpoint/census, archive disposition/artifact digest, rollback
mode/forward-snapshot proof, operation id, JTI, deadline, and authenticated
source-lifecycle audience. Before any external routing/storage/archive/key/backup
destructive step, the source operator SHALL consume it under source
lifetime/fencing authority and revalidate expiry, unchanged checkpoint/census,
exact disposition/artifact, current destination verification/recovery/no-loss,
operation id/JTI, and fence. Drift, expiry, replay, or mismatch SHALL authorize
nothing and change nothing. Clearance issuance, source consumption, and external
`source-retirement-completion/v1` attestation SHALL be distinct evidence;
clearance SHALL reserve the retirement approval JTI into one opaque
retirement-lifecycle journal/ref. `retire-source(phase="finalize")` SHALL be a
new idempotent mutation that references that exact journal and plan, forbids a
second retirement token, and verifies `source-retirement-completion/v1` from
configured source-lifecycle/control trust. Completion SHALL bind lifecycle ref,
clearance JTI/digest, source vault/installation/generation and consumed fence,
exact disposition/artifact digest, source-operator completion operation id,
content-free outcome/time, source consume event id/digest and verified source
receipt-head digest, issuer/audience, and authentication-proof digest.
Only then MAY finalize convert the already-active pending rollback fence into
the permanent post-retirement rollback rule. Exomem SHALL
not perform external destruction.

Forward-only consumption SHALL additionally revalidate the already-durable
destination pending rollback-fence digest plus exact forward snapshot/ledger;
without it no external destructive step is authorized.

#### Scenario: Rehearsal token is presented to real cutover

- **WHEN** a caller tries to reuse a cloned-rehearsal token, attestation, snapshot, or authority for a real destination
- **THEN** real-cutover admission refuses the clone/run-mode mismatch
- **AND** fresh real fingerprints, plan, attestations, and owner confirmation are required

#### Scenario: Cutover completes without retirement approval

- **WHEN** a real cutover verifies and unseals successfully
- **THEN** the source remains an unchanged required recovery asset
- **AND** no source-retirement clearance or destructive source action is implied

#### Scenario: Retirement prerequisites are all current

- **WHEN** rehearsal including rollback, separately approved real cutover, current destination verification/preimage, unchanged authenticated source checkpoint, exact archive disposition, and fresh retirement-specific confirmation all verify
- **THEN** `retire-source` may append a content-free clearance receipt
- **AND** the response states that actual source-side retirement remains the source operator's responsibility

#### Scenario: Retirement authorizes later archive destruction

- **WHEN** the exact retirement preview declares external archive destruction and the owner separately confirms that disposition
- **THEN** clearance states precisely which destination bytes, owner-only mappings/digests, and plaintext-free receipts will remain
- **AND** it states that source-only bytes, control state, and archive-only provenance cannot be reconstructed after the external operator destroys the archive

#### Scenario: Forward-only retirement preserves imported bytes

- **WHEN** retirement would make the source and archive irrecoverable
- **THEN** clearance requires a separately retained verified forward snapshot, a pre-issued pending pre-cutover rollback fence that finalization makes permanent, and a surviving-copy ledger for every imported bundle
- **AND** any imported bundle with zero post-effect copies blocks clearance and consumption

#### Scenario: Source operator consumes clearance after drift

- **WHEN** the source census, archive disposition, destination verification/no-loss proof, deadline, JTI, or fencing authority differs at consumption
- **THEN** the single-use clearance is refused without authorizing or performing a destructive step
- **AND** clearance issuance evidence remains distinct from consumption and external completion evidence

#### Scenario: Custodian receipt expires before rollback commit

- **WHEN** a reviewed rollback counted a custodian archive but its receipt expires, signer is revoked, retention terms drift, or archive digest no longer verifies before commit
- **THEN** rollback recomputes the surviving-copy ledger and refuses before restore if no other verified copy preserves every imported bundle
- **AND** a prior plan-time verification or still-matching opaque receipt reference is not sufficient

### Requirement: Consolidation remains deterministic within the Exomem product boundary

The server SHALL accept finite structured reconciliation choices, policy
documents, probes, and exact fingerprints and SHALL deterministically validate
and enforce them. It SHALL NOT add a server-side reasoning model, infer semantic
merges, silently author policy, or decide unresolved C4-C8 conflicts. Agent-side
reasoning MAY propose choices but SHALL not constitute approval or enforcement.

Claims of sealing, governance, verification, or non-disclosure SHALL apply only
to Exomem-mediated product commands and surfaces. Direct filesystem reads or
writes, manual copy/paste, direct object-store access, and uploads to an external
model outside Exomem SHALL be explicitly documented as outside that enforcement
boundary and SHALL NOT be reported as governed or verified by a consolidation
run.

#### Scenario: Agent proposes a conflict decision

- **WHEN** an agent proposes a path, identity, relation, or policy resolution
- **THEN** the server validates it against the finite reconciliation schema and exact snapshots
- **AND** unresolved or invalid choices remain blocked until owner review rather than being decided by a server model

#### Scenario: Operator bypasses Exomem

- **WHEN** an operator reads staging through the filesystem, pastes private text manually, or uploads bytes directly to an external service
- **THEN** consolidation evidence makes no release-plane enforcement claim for that action
- **AND** product documentation and operational results preserve the boundary explicitly
