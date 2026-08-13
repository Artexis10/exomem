## ADDED Requirements

### Requirement: Consolidation exports carry externally authenticated source claims

When a completed portable export is designated for consolidation, the
portability boundary SHALL calculate the canonical source-census SHA-256 and
SHALL bind the otherwise self-consistent archive to source authority using
either an authenticated transport receipt with an exact trusted issuer record
or a detached Ed25519 `source-export-attestation/v1`. The external proof
SHALL bind the source logical vault identity, source installation id/generation
and active-fence digest, export operation identity, quiescence checkpoint, archive SHA-256, manifest
SHA-256, canonical source-census SHA-256, issued time, expiry time, and signer
key identifier. A Hosted proof SHALL additionally bind its typed routing
`source_cell_id`; a standalone local proof SHALL omit that Hosted-only field.
`source_cell_id`, `source_vault_id`, and `source_installation_id` SHALL be
distinct typed namespaces and equality between cell and vault ids SHALL be
malformed. The proof SHALL be delivered separately from the archive through an
opaque authorized reference.

The source content census SHALL structurally exclude every `_Consolidation/**`
subtree, receipt chain, seal/journal/control state, runtime/derived state, and
private staging. Archive and manifest hashes SHALL still bind every admitted
archive byte separately; the census exclusion SHALL not make control/evidence
portable content or weaken artifact-integrity verification.

The archive SHALL NOT contain the credential, signing key, tenant encryption
key, or reusable transport authority that created the proof. An archive's
manifest, a caller-typed expected hash, or a signature key supplied in the same
request SHALL NOT establish source authenticity. Trust roots and authenticated
channels SHALL come from destination/control-plane configuration and SHALL
support rotation without sharing a source HMAC secret.

Ed25519 SHALL sign
`u32be(len("exomem.source-export-attestation/v1")) ||
"exomem.source-export-attestation/v1" || u64be(len(jcs_claim_bytes)) ||
jcs_claim_bytes`; the signature SHALL not occur in the claim object and the raw
64-byte signature SHALL use unpadded base64url. The private key SHALL remain under
source cell/machine custody. Destination
`SourceExportVerifierRecord/v1` SHALL bind the raw 32-byte Ed25519 public key in
unpadded base64url and key id `ed25519-sha256:` plus lowercase SHA-256 hex of
those raw bytes, purpose=`vault-consolidation-export`, audience/trust domain, allowed source
vault/installation/generation and applicable typed Hosted cell, status,
not-before/not-after, revocation, and registry generation. Records SHALL remain
in private no-follow destination/control-plane state outside archives, requests,
and recallable knowledge. Intake, apply,
retirement clearance, and retirement consumption SHALL independently verify the
signature and current trust record. Rotation overlap SHALL enumerate both
accepted public records; revoked/premature/expired/wrong-purpose/audience/source
keys fail. Required public-key/revocation history SHALL remain verifiable through
retirement/evidence retention. RFC 8032 and canonical-claim fixed vectors SHALL
pin cross-runtime behavior. A named control receipt alternative SHALL bind the
same claims through an equally closed issuer/purpose/audience/source/validity/
revocation record; mere transport authentication does not qualify.

`jcs_claim_bytes` SHALL use the consolidation RFC 8785 closed value subset:
NFC-before-validation strings, duplicate/post-NFC-duplicate key refusal, JCS
escaping/order, RFC3339 millisecond timestamp strings, bounded non-negative
integers only, and no floats/exponents/NaN/infinity/unspecified null. Fixed
vectors SHALL pin the framed bytes and signature across runtimes.

#### Scenario: Control plane authenticates a consolidation export

- **WHEN** quiesced export verification succeeds and the trusted control plane issues a receipt over the exact source, archive, manifest, census, operation, and checkpoint claims
- **THEN** it returns separate opaque archive and authentication-proof references plus content-free integrity metadata
- **AND** the archive remains unchanged and inaccessible without authorized artifact delivery

#### Scenario: Attacker recomputes an archive manifest

- **WHEN** an archive and its internal manifest agree but its detached proof is absent, signed by an untrusted key, expired, or bound to a different source or digest
- **THEN** consolidation intake authenticity fails
- **AND** self-consistency and a caller-supplied expected hash do not upgrade the artifact to trusted source state

### Requirement: Portable source identity covers standalone local cells

The consolidation export contract SHALL work for both Hosted bindings and
standalone local cells. A local source SHALL read its logical `vault_id`,
`installation_id`, and authenticated root binding from private no-follow state
outside `Knowledge Base/` and SHALL authenticate the detached export claims with
a cell/machine-owned key or trusted local control transport. The private key,
identity record, installation ownership registry, and root binding SHALL not be
included in the archive. The destination SHALL verify against configured
machine/cell trust or an authenticated control channel and SHALL not accept a
request-supplied key, id, binding, or trust root.

Export SHALL fail closed when a local vault has not completed owner-authorized
legacy identity adoption, when the private identity record is copied/malformed,
when its installation id is claimed by another root/cell, or when the stable
root/filesystem identity or registry fence does not match its authenticated
binding. The adoption census SHALL be immutable provenance, not a live identity
guard: legitimate writes SHALL not require re-MAC, and export SHALL compute/sign
the current census only after quiescence. An owner-authorized move
of the same installation MAY update the stable root binding while preserving its
logical and installation ids. An explicit rehearsal clone SHALL instead
generate new active ids and carry immutable clone-of ids/snapshot provenance in
its authenticated proof.

#### Scenario: Standalone local source exports

- **WHEN** a locally bound source reaches quiescence and its private identity/root binding verifies
- **THEN** the cell/machine-owned trust signs or authenticates the exact export claims without exposing its private key or identity state in the archive
- **AND** the destination can authenticate the logical source and serving installation without accepting caller-selected ids

#### Scenario: Identity record was copied to another root

- **WHEN** export observes the same installation id claimed by another root/cell or a stable root/fence that does not match the authenticated binding
- **THEN** export fails before issuing an attestation or completed artifact
- **AND** a caller-supplied replacement id, key, or expected binding cannot repair the collision

#### Scenario: Explicit clone exports for rehearsal

- **WHEN** an approved clone operation creates a rehearsal cell from a source snapshot
- **THEN** its export attestation names distinct active logical/installation ids plus immutable clone-of ids and snapshot digest
- **AND** it cannot be confused with an in-place move, failover restore, or the real source installation

#### Scenario: Legitimate content changes after adoption

- **WHEN** an adopted local vault receives an ordinary write and is later quiesced
- **THEN** its stable identity binding remains valid and export signs the newly computed current census
- **AND** equality with the immutable adoption census is not required

#### Scenario: Hosted routing identity aliases logical vault identity

- **WHEN** a Hosted binding or export proof serializes the same identifier as `source_cell_id` and `source_vault_id`
- **THEN** portability validation refuses the typed binding before attestation or readiness
- **AND** no routing identifier is accepted as logical vault provenance

### Requirement: Logical-id-preserving restore uses a fenced installation transfer

An identity-aware portability restore that preserves logical `vault_id` SHALL
mint a fresh target `installation_id` and monotonic generation N+1. The target
SHALL generate a challenge, and authenticated
`vault-identity-transfer/v1` SHALL bind logical vault, source installation and
generation N, target installation/challenge, exact export/census/checkpoint,
operation, validity, and target generation. A configured authoritative registry
SHALL compare-and-swap fence/deactivate source N and reserve target N+1 before
target readiness. Activation SHALL consume the reservation; stale source
readiness/export/mutation SHALL reject its fence.

Recovery SHALL advance only exact source-active, source-fenced/target-pending,
or target-active states. Two active installations, skipped generation,
caller-selected installation, stale transfer, or unavailable fencing authority
SHALL fail closed. Without authoritative fencing an offline candidate may be
prepared but not served under the same logical id; it requires later fencing or
explicit new-lineage adoption. Runtime binding/private identity remains outside
the archive and target private state remains fresh, preserving the canonical
portability rule.

#### Scenario: Failover crashes after fencing source

- **WHEN** source generation N is fenced and target N+1 is reserved but target activation has not completed
- **THEN** retry under the same transfer operation may activate only the exact prepared target
- **AND** source remains inactive and no second target/generation can claim the logical vault

#### Scenario: Fencing authority is unavailable

- **WHEN** a restore cannot prove deactivation of the prior active installation
- **THEN** it may retain an offline candidate but cannot advertise readiness under the preserved logical vault id
- **AND** copying source binding state does not bypass the fence

### Requirement: Verified portability archives are reusable as private bounded consolidation intake

The portability validator SHALL expose a reusable validation/extraction seam for
consolidation that applies the existing version, resource-bound, manifest,
entry-digest, unsafe-path, traversal, link, duplicate-normalized-path,
cross-platform case-collision, and unsupported-entry checks. Consolidation use
SHALL extract only into its private artifact store outside `Knowledge Base/` and
every active vault root. It SHALL return a bounded canonical inventory plus
opaque artifact references and digests; it SHALL NOT publish the extraction as a
restored cell or move it over the destination.

The reusable seam SHALL keep the authenticated archive immutable and SHALL
reject source runtime binding, credential, lifecycle, lease, idempotency,
replay, temporary, log, or derived-index entries under the existing portability
rules. Consolidation SHALL consume authored canonical bytes and SHALL rebuild
derived state after publication. This seam SHALL NOT weaken the existing ban on
live or in-place overlay restore.

#### Scenario: Valid archive is opened for reconciliation

- **WHEN** an authenticated supported archive passes every portability and resource-bound check
- **THEN** canonical entries are available to consolidation only through hashed private artifacts and a bounded inventory
- **AND** no archive byte is written into the active destination or made visible to ordinary recall

#### Scenario: Consolidation targets the active root as restore staging

- **WHEN** a request attempts to use restore publication, the active destination, or a `Knowledge Base/` subtree as consolidation extraction
- **THEN** portability admission refuses before extracting or publishing an entry
- **AND** the existing offline-new-target restore contract remains unchanged

#### Scenario: Archive includes a source-derived database

- **WHEN** a consolidation archive declares an embedding, lexical, graph, freshness, CLIP, lease, credential, or other runtime/derived entry
- **THEN** the validator refuses the archive under portability inclusion rules
- **AND** consolidation does not copy the entry even if its digest matches the manifest

### Requirement: Source checkpoint release and retirement handoff are explicit

An export quiescence checkpoint used by consolidation SHALL have one explicit
authorized disposition: release the source back to its prior admission state,
retain or reacquire quiescence for an exact cutover snapshot, or hand the
checkpoint to a separately authorized source-retirement lifecycle operation.
The current source-retention proof SHALL state content-free source identity,
checkpoint identity, census digest, quiescence/routing state, issue/expiry, and
outcome. It SHALL NOT claim continued quiescence or unchanged source bytes after
ordinary source admission has resumed.

Real-cutover admission and source-retirement clearance SHALL verify that the
source still matches the authenticated snapshot bound to the approved plan. If
the source was released and its newly quiesced canonical census differs, the
plan and retirement eligibility SHALL be stale and a new reconciliation and
owner-reviewed cutover SHALL be required. A cloned rehearsal MAY release its
clone checkpoint after its required rollback, but that release SHALL not count
as real-source retirement authority.

Retirement clearance SHALL be a single-use source-lifecycle capability consumed
under the source lifetime/fencing boundary before any external destructive step.
Consumption SHALL reverify purpose/audience, operation id/JTI/deadline, unchanged
source checkpoint/census, exact archive disposition/artifact digest, destination
verification/recovery/surviving-copy proof, post-retirement rollback mode and
forward snapshot where required, current source fence, and for forward-only the
already-durable destination `retirement-pending-forward-only/v1` fence digest.
Drift, expiry,
replay, or mismatch SHALL change nothing. Clearance issuance, successful
consumption, and external completion attestation SHALL be separate idempotent
records; portability SHALL not itself delete external storage, archive, backup,
key, account, or billing state.

The external completion record SHALL be authenticated
`source-retirement-completion/v1` from configured source-lifecycle/control trust
and bind lifecycle ref, clearance JTI/digest, source vault/installation/
generation and consumed fence, exact disposition/artifact digest, completion
operation id, content-free outcome/time, source consume event id/digest and
verified source receipt-head digest, issuer/audience, and proof digest.
Caller-authored completion or completion for another consumed clearance SHALL
not finalize destination retirement state.

A declared custodian-transfer disposition SHALL require an independently
authenticated `archive-custody-receipt/v1`, delivered only by opaque protected
reference. Its closed JCS claims SHALL require exactly `schema`,
`custodian_id_digest`, `retention_domain_digest`, `source_vault_id_digest`,
`source_installation_id_digest`, `source_installation_generation`,
`archive_digest`, `manifest_digest`, `source_census_digest`,
`transfer_operation_id`, `retention_terms_digest`, `accepted_at`, `issued_at`,
`not_before`, `expires_at`, `nonce`, and `signer_key_id`. The detached Ed25519
signature SHALL cover
`u32be(len("exomem.archive-custody-receipt/v1")) ||
"exomem.archive-custody-receipt/v1" || u64be(len(jcs_claim_bytes)) ||
jcs_claim_bytes`; the raw 64-byte signature SHALL be outside the claims and
encoded as unpadded base64url. `not_before <= accepted_at <= issued_at <
expires_at` SHALL hold, and every consuming gate SHALL run before `expires_at`.

The private `ArchiveCustodianVerifierRecord/v1` registry SHALL have exactly
`schema`, `algorithm`, `key_id`, `public_key`, `purpose`,
`custodian_id_digest`, `retention_domain_digest`, `source_vault_id_digest`,
`source_installation_id_digest`, `source_installation_generation`,
`destination_audience_digest`, `status`, `not_before`, `not_after`,
`registry_generation`, and conditionally `revoked_at` plus
`revocation_reason_digest` exactly when `status=revoked`. `status` SHALL be
`active|inactive|revoked`; `registry_generation` SHALL be an integer in
`0..2^53-1`; `not_before < not_after` SHALL hold. `algorithm` SHALL be
`Ed25519`; `public_key` SHALL be the raw
32-byte Ed25519 public key in unpadded base64url and `key_id` SHALL be
`ed25519-sha256:<lowercase-sha256-hex>`; `purpose` SHALL equal
`vault-consolidation-archive-custody`. The remaining fields bind the exact allowed custodian and retention
domain, allowed source lineage, destination trust audience, status,
not-before/not-after, revocation time/reason, and monotonic registry generation.
Unknown, inactive, premature, expired, revoked, wrong-purpose, wrong-audience,
wrong-custodian/domain, or wrong-lineage records SHALL fail. Rotation SHALL
permit only an explicitly bounded two-key overlap and retain verification and
revocation history for the declared evidence-retention period. Fixed valid,
revoked, expired, wrong-domain, wrong-artifact, and cross-runtime signature
vectors SHALL be normative.

Retirement-plan materialization, clearance issuance, clearance consumption,
and every rollback-plan materialization and rollback commit that counts the
custodian archive as a surviving copy SHALL independently re-read the protected
receipt, verify its exact signature and current registry state, re-hash the
archive/manifest/census, and revalidate current retention terms and validity.
A receipt valid at an earlier gate SHALL not be grandfathered through expiry,
revocation, artifact loss, changed terms, or a registry mismatch; the affected
operation SHALL perform no lifecycle or restore effect.

The source lifecycle SHALL never consume a forward-only clearance whose pending
destination rollback fence is absent or mismatched. Lost consume/completion
acknowledgement leaves that destination fence active; it may be released only by
recovery proof that the JTI was never consumed and the source/archive still
exists.

#### Scenario: Real source changes after export release

- **WHEN** source admission resumes, a canonical source write occurs, and the source is later quiesced for cutover or retirement
- **THEN** its census no longer matches the plan-bound authenticated checkpoint and the cutover or retirement gate refuses
- **AND** no unchanged-source claim is inferred from the old archive

#### Scenario: Retirement claims the unchanged checkpoint

- **WHEN** a separately confirmed retirement operation presents the still-current authenticated source checkpoint after verified real cutover
- **THEN** portability records a content-free handoff to the source lifecycle operator
- **AND** Exomem itself does not delete storage, backups, keys, account data, billing state, or the source archive

#### Scenario: Checkpoint disposition is retried

- **WHEN** the same authorized release, retention, or retirement-handoff operation is retried with the same operation identity and payload
- **THEN** portability returns the same disposition terminal idempotently
- **AND** a changed checkpoint, source census, target run, or disposition conflicts rather than being adopted

#### Scenario: Retirement clearance is replayed

- **WHEN** a source operator reuses an already consumed JTI or changes its checkpoint, disposition, archive digest, no-loss proof, or fencing context
- **THEN** source-lifecycle admission refuses before any destructive external step
- **AND** issuance evidence is not confused with consume or external completion evidence

#### Scenario: Custodian proof is revoked after retirement planning

- **WHEN** an archive-custody receipt was valid during retirement-plan rendering but its verifier record is revoked before clearance or source consumption
- **THEN** the current gate removes that archive from the surviving-copy ledger and refuses when no other verified survivor remains
- **AND** prior approval, a cached verification result, or matching receipt bytes cannot authorize the effect

#### Scenario: Rollback counts an authenticated custodian archive

- **WHEN** a rollback plan or commit depends on the custodian copy to preserve imported bytes
- **THEN** it independently verifies the exact custodian, retention domain/terms, transfer operation, archive, manifest, source census, validity, and current signer status
- **AND** a wrong artifact, expired receipt, missing registry history, or unavailable archive blocks before restoration
