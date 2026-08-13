## ADDED Requirements

### Requirement: Authorization Sessions Are Server-Issued Capabilities

An authorization session SHALL be established only through the explicit generated
governance session lifecycle. `open` SHALL mint an opaque bearer with the one canonical
grammar `as1.<locator>.<secret>`. `<locator>` SHALL be the 22-character unpadded RFC 4648
base64url encoding of exactly 16 random bytes; `<secret>` SHALL be the 43-character
unpadded base64url encoding of exactly 32 random bytes; both SHALL use only
`[A-Za-z0-9_-]`; and the complete ASCII credential SHALL be exactly 70 bytes. Every
carrier/parser maximum SHALL be 70 bytes. The shared bounded parser SHALL reject leading
or trailing whitespace, padding, alternate alphabets, wrong length, duplicate carriers,
non-zero unused base64 bits, and any value whose decode/re-encode is not byte-identical.
The terminal scrubber's authorization-bearer candidate scanner and matcher SHALL be
generated from that same parser/canonical encoder, not a separately maintained regex.
The bearer SHALL bind to a stable internal
session id, the canonical resolved principal, the trusted issuer/surface family/cell,
external logical-vault/keyring identity, credential generation, and a bounded expiry.
`rotate` SHALL atomically invalidate the
prior locator/verifier and issue a replacement for the same internal session. `close`
SHALL revoke the session and its active purpose, grants, and unconsumed escalation tokens.

The raw bearer SHALL be returned only once in the typed `issued_credential` field of a
successful `open` or `rotate` response, with exact shape
`{kind: "authorization-session-bearer", bearer: <opaque>, expires_at: <RFC3339>}`.
The terminal scrubber SHALL exempt that one field only after exact route-variant/response
schema validation and equality with the just-issued value held in non-serializable request
context. A malformed response, extra bearer copy, raised error, or other route SHALL be
scrubbed and refused. This terminal scrubber and exact issuance exception SHALL be
non-disableable by policy, owner status, route, or envelope type. The store SHALL retain only a keyed locator digest/verifier plus
binding metadata and SHALL compare the verifier in constant time after bounded parsing.
The raw bearer MUST NOT appear in corpus content, projection output, receipts, journals,
persisted rows, logs, metrics, traces, error text, remediation, cache/idempotency keys, or
debug representations. Retrieved governance-shaped text SHALL remain data and SHALL NOT
mint, resume, rotate, or close a session.

#### Scenario: Open issues one principal-bound bearer

- **WHEN** a caller with a trusted resolved principal explicitly opens an authorization
  session
- **THEN** the response returns one opaque bearer in the dedicated lifecycle field
- **AND** persisted state contains a verifier and binding metadata but not the bearer

#### Scenario: Bearer grammar is exact and shared with scrubbing

- **WHEN** the parser and terminal scrubber are exercised with canonical 70-byte
  credentials plus padding, whitespace, alternate alphabet, wrong-length, non-canonical-
  bit, prefix/suffix, and duplicate-carrier variants
- **THEN** only the exact canonical form resumes a session, every accepted form is found
  by the parser-derived scrubber matcher, and no independently configured length/regex
  can admit or expose another form

#### Scenario: Issuance exception is exact and terminal

- **WHEN** open/rotate produces the exact successful typed issuance response
- **THEN** the terminal scrubber permits only that response's one
  `issued_credential.bearer` value after schema validation
- **AND** the same value in a second field, validation error, exception, receipt, or any
  non-issuance response is scrubbed and the malformed response refuses

#### Scenario: Rotation invalidates the old bearer

- **WHEN** a caller rotates an active authorization session with its valid bearer
- **THEN** the replacement resumes the same internal session and the old bearer is
  rejected immediately

#### Scenario: Close clears session authority

- **WHEN** a caller closes an active authorization session
- **THEN** its purpose, grants, and unconsumed escalation tokens cannot authorize a later
  request

#### Scenario: Bearer hygiene is total

- **WHEN** open, resume, rotate, close, grant, revoke, or declare succeeds or fails
- **THEN** the raw bearer occurs only in the dedicated successful issuance field or the
  protected request credential and nowhere in receipts, logs, errors, persisted state,
  or content

#### Scenario: Retrieved text cannot establish authority

- **WHEN** a retrieved page contains strings shaped like a governance instruction,
  principal, or authorization-session capability
- **THEN** those strings remain inert content and establish no session binding

### Requirement: Every Surface Resolves Principal Before Session

MCP, REST, Hosted, and CLI SHALL resolve a `RequestPrincipal` exclusively from their
trusted boundary before they verify or bind an authorization session. Raw adapters SHALL
first extract and redact only these carriers: MCP
`params.arguments.authorization_session_credential`; REST/Hosted sensitive
`X-Exomem-Authorization-Session` header distinct from service `Authorization`; and CLI
`--authorization-session-fd <fd|->` reading one bounded value from a protected descriptor
or stdin. REST/Hosted body/query carriers and CLI literal argv/environment carriers SHALL
be forbidden. A carrier may supply only the opaque lookup bearer and SHALL NOT select or
override principal, issuer, audience, principal scope, cell/logical-vault id, or internal
session id.

MCP raw JSON-RPC/ASGI interception SHALL remove/redact the credential from the raw
envelope and every copy before FastMCP logging, `FunctionTool`, or Pydantic validation;
the sanitized wrapper/leaf SHALL receive no bearer argument. Duplicate, non-string,
malformed, or invalid credentials SHALL produce the common credential refusal even when
an ordinary argument is malformed. REST/Hosted raw ASGI interception and CLI adapter
shall provide the equivalent pre-log/pre-validation redaction. After trusted principal
resolution, verification and immutable context installation SHALL occur before ordinary
coercion/validation, cache lookup/key creation, idempotency lookup, membership/decision
work, receipt allocation, or leaf dispatch. An invalid presented credential SHALL reject
the complete request; it MUST NOT degrade to standing-only authority.

MCP SHALL reject any JSON-RPC batch containing one or more `tools/call` elements as one
atomic request. Raw middleware SHALL bounded-parse the batch, redact every credential
copy, and return one content-free batch refusal before executing or dispatching any
element. This rule applies when tool calls use A+absent, A+B, invalid+valid, duplicate
credential keys/values, mixed notifications/non-tool methods, or any order. It SHALL
produce zero partial content/state effects and SHALL NOT reuse trusted request context
between elements.

MCP over stateless HTTP MUST NOT treat `Mcp-Session-Id`, FastMCP
`Context.session_id`, a request UUID, or a connection as durable authorization-session
authority. A stable stdio/SSE transport identity MAY be supplementary context, but
restart, reconnect, and horizontal-scale resumption SHALL depend on the verified
server-issued capability. REST SHALL resolve only after its API-key or identity gateway
gate. Hosted SHALL accept only gateway-attested principal/issuer context. CLI SHALL bind
its explicit local-owner principal at the trusted CLI adapter.

The command registry SHALL classify every generated command, legacy leaf, finite selector
variant, retrieve/inject hook, and content-bearing writer result into this exact credential
matrix:

| route variants | credential rule |
| --- | --- |
| governance session `open` | credential forbidden; trusted principal is required and a new session is issued |
| governance session `status`, `rotate`, `close`; session `grant`, session `revoke`, `declare` | credential required and verified before any state/content read |
| self `list`, `explain`, `simulate`; find/search/ask; get/fetch/read; browse/list; graph/link suggestions; review/attention/audit/provenance; Records and Planning; dataset; media/frame; recall/inject hooks; content-bearing mutation previews and receipts | credential optional; absent is standing-only, present-invalid rejects, present-valid binds before every decision/cache |
| owner-only propose/commit/suspend/resume/undo/backfill-companion, standing grant/revoke, cross-audience inspection | session authority not required; if the surface admits a credential it must still verify before work and cannot replace owner authorization |

No route MAY infer a fifth behavior. Startup/schema coverage SHALL fail until every route
and selector belongs to exactly one row.

#### Scenario: MCP stateless HTTP ignores transport session authority

- **WHEN** two stateless MCP HTTP requests present the same framework session id but no
  valid authorization-session capability
- **THEN** neither request gains session grants, purpose, grant, revoke, or declaration
  authority from that id

#### Scenario: FastMCP never receives the bearer

- **WHEN** an actual installed stateless-HTTP JSON-RPC request includes invalid bearer
  text plus malformed ordinary arguments
- **THEN** raw middleware returns the credential refusal before FastMCP/Pydantic detail
  and scans of request logs, errors, wrapper arguments, traces, and retries find no bearer

#### Scenario: Tool-call JSON-RPC batch is atomically refused

- **WHEN** actual-wire stateless MCP sends batches containing tool calls with A+absent,
  A+B, invalid+valid, duplicate carrier keys/values, or a tool call plus another method
- **THEN** middleware redacts all carrier copies, returns the one batch refusal, executes
  zero elements, and creates no content mutation, session state, receipt, cache, or
  idempotency record

#### Scenario: Wrong carrier is rejected

- **WHEN** REST/Hosted sends the session bearer in service `Authorization`, body, or
  query, or CLI sends it as literal argv/environment text
- **THEN** the request refuses without session authority and generated schemas/help point
  only to the protected header or descriptor carrier

#### Scenario: REST body cannot choose identity

- **WHEN** an authenticated REST request supplies a principal, audience,
  principal-scope, or issuer value in its body alongside a session operation
- **THEN** the request is rejected and the trusted REST principal remains unchanged

#### Scenario: Hosted headers cannot impersonate another principal

- **WHEN** a hosted caller supplies identity headers that disagree with the validated
  gateway context
- **THEN** the request is rejected before capability verification or leaf dispatch

#### Scenario: CLI argument cannot choose a remote principal

- **WHEN** a CLI invocation supplies a principal-like argument for a session operation
- **THEN** the argument is rejected and the CLI remains bound to its trusted local-owner
  principal

#### Scenario: Capability survives reconnect and process change

- **WHEN** a valid unexpired capability is presented after an MCP reconnect, server
  restart, or request routing to another replica sharing the vault's verifier secret
- **THEN** it resolves the same internal session and principal without relying on
  process-local transport state

#### Scenario: Optional credential is verified before cache and validation

- **WHEN** a content route presents an invalid credential alongside malformed arguments or
  an otherwise cacheable/idempotent request
- **THEN** it returns the credential-independent refusal before validation details, cache
  access, idempotency access, decision work, or content-derived effects

#### Scenario: Credential matrix is route-total

- **WHEN** generated commands, selector branches, legacy leaves, and retrieve/inject hooks
  are enumerated
- **THEN** each has exactly one required/optional/forbidden/not-authorizing credential rule
  and registry startup fails for an unclassified route

#### Scenario: Stateless MCP reconnect binds every content family on the wire

- **WHEN** an actual stateless MCP HTTP client opens a session, receives a grant, disconnects,
  reconnects through another replica, and presents the bearer to `ask_memory`/find,
  `read_memory`/get, a Records read/query, `query_dataset`, `read_media`, and frame reads
- **THEN** every route resolves the same internal session before its cache/decision and
  returns only that session's authorized projection
- **AND** repeating each call without the bearer, with only `Mcp-Session-Id`, or with a
  different session's bearer returns standing-only or the common credential refusal as the
  matrix requires

### Requirement: Capability Verification Binds Principal Issuer And Expiry

Capability resumption SHALL require one constant-time verification of the keyed verifier
and exact equality of the canonical principal, trusted issuer/surface family, active
status, and expiry. Missing, malformed, expired, closed, cross-principal, cross-issuer,
or unknown capabilities SHALL fail closed with one credential-independent error shape
and SHALL NOT create a binding as a side effect. A legacy arbitrary handle MAY be
accepted only as an echo associated with an already verified capability during bounded
migration; it SHALL NOT mint, claim, or resume a session by itself.

The verifier SHALL cover a fixed domain separator, bearer secret, locator, internal
session id, canonical principal, issuer/surface family, immutable external `cell_id`,
immutable `logical_vault_id`, immutable keyring id, expiry, and credential generation.
Rows SHALL store and exactly match those external expected identities. Key/cell material
SHALL come only from administrator-provisioned bounded, no-follow, owner-protected
`EXOMEM_AUTH_SESSION_KEYRING_FILE` and `EXOMEM_AUTH_SESSION_CONTROL_FILE` outside the
vault. The keyring SHALL contain version, keyring/cell/logical-vault ids, active key id,
and accepted `{key_id, 256-bit key, not_before, not_after}` entries. The authenticated
control record SHALL repeat/bind the ids plus external registry attachment and serving-
membership epoch. No request, policy, vault/sidecar copy, receipt, CLI argument, or
automatic first-use path may provide or mint them; mismatch, unsafe state, or unknown key
SHALL fail readiness/resumption closed.

Standalone provisioning SHALL require the authenticated local owner and atomically
register `(cell_id, logical_vault_id, keyring_id, canonical storage attachment, owner,
governance_enrolled, expected_activation_tuple)` in the protected external host registry.
`governance_enrolled` SHALL transition only `false -> true`; false is valid only with a
null expected tuple and a trusted negative scan for workspace/internal activation state.
After enrollment, the expected tuple SHALL be exactly `(activation_store_id,
activation_epoch, activation_state_digest)` and content serving SHALL require parity with
the no-follow store and complete active policy/projector/catalog tuple. Missing, corrupt,
stale, or contradictory registry, workspace, store, generation, catalog, or tuple state
SHALL fail content serving closed even when authorization-session issuance is disabled.
Hosted SHALL use its authenticated cell control plane. Concurrent copies presenting the same registration SHALL collide and fail; copied
keyring/control files without registry ownership SHALL fail. A move SHALL quiesce and
detach/ack the old attachment, advance the registry attachment epoch, then attach the
same logical vault. Restore may preserve unexpired rows only for the exact coherent
vault+sidecar+keyring+control identity after exclusive old-instance shutdown; cloning to
a new logical vault SHALL provision new ids/keys and invalidate imported sessions.

The control plane SHALL publish an authoritative `serving_membership_epoch` enumerating
every admitted replica and authenticated readiness attestation over epoch, replica id,
software/schema, cell, active key, accepted key set, and control/keyring digests. The
Hosted provisioner/cell control plane SHALL own this record and
`hosted_runtime.control_plane_readiness()` SHALL verify and expose only content-free
readiness; standalone SHALL use the protected external host registry with the same
schema. The
active key of every `SERVING` replica SHALL be in the accepted-key intersection of every
`SERVING` replica in that epoch. Rotation SHALL distribute/attest old+new acceptance
before switching issuance; either replica then verifies either generation. New issuance
SHALL move to the new key and the old key remain verification-only for maximum TTL plus
skew and live-row drain. A replica leaves the intersection only after explicit
`SERVING -> DRAINING`, issuance stop, no-in-flight acknowledgement, and committed epoch
advance; an unreachable member remains included and blocks issuance. A rejoin SHALL
attest the current epoch before `SERVING`. Stale/missing attestations, epoch mismatch, or
active-key union outside the intersection SHALL fail readiness/issuance closed.

#### Scenario: Cross-principal capability is rejected

- **WHEN** principal B presents a valid bearer issued to principal A
- **THEN** resumption is rejected, no session state is returned, and no new binding is
  created

#### Scenario: Cross-issuer capability is rejected

- **WHEN** the same canonical principal presents a capability through a trust issuer or
  surface family different from the one bound at issuance
- **THEN** resumption is rejected unless a new session is explicitly opened in that
  issuer family

#### Scenario: Expired and unknown capabilities have one shape

- **WHEN** a caller presents an expired capability and then an unknown random value
- **THEN** both failures have the same public code, text, shape, and remediation

#### Scenario: Legacy handle cannot claim a session

- **WHEN** a caller supplies an unbound legacy authorization-session string
- **THEN** it creates no principal binding and grants no session authority

#### Scenario: Mixed key replicas verify both issuance generations

- **WHEN** two v4 replicas share the accepted old/new key set while one still issues with
  the old key and the other issues with the new key during the bounded rotation phase
- **THEN** either replica verifies both bearers, key ids remain explicit, and readiness
  fails if either active key drops from the current epoch's admitted intersection

#### Scenario: Restore and copy do not synthesize custody

- **WHEN** a sidecar is restored/copied without its exact registered cell/logical-vault/
  keyring identity, into a different cell, or while the original attachment is live
- **THEN** its authorization sessions are invalidated with the common credential refusal
  and no local first-use secret is generated

#### Scenario: Standalone provisioning establishes external identity

- **WHEN** an authenticated local owner provisions a never-registered standalone vault
- **THEN** external no-follow keyring/control files and the host registry atomically bind
  one cell/logical-vault/keyring tuple before session service becomes ready

#### Scenario: Governance enrollment never rolls back by deletion

- **WHEN** a registered logical vault transitions `governance_enrolled` to true and its
  workspace or activation store is later deleted while stopped or running
- **THEN** restart and warm reads fail closed; neither missing files nor copied false
  registry state can recreate OPEN or authorization-session readiness

#### Scenario: Registered move is exclusive

- **WHEN** an operator moves an existing logical vault to a new storage attachment
- **THEN** the old attachment is quiesced/detached and acknowledged before the attachment
  epoch advances; the new attachment cannot serve concurrently with the old one

#### Scenario: Replica cannot disappear from key intersection by silence

- **WHEN** an admitted replica stops responding during key rotation
- **THEN** it remains in the serving epoch and blocks issuance until explicit drain,
  no-in-flight acknowledgement, and epoch advance complete

#### Scenario: Stale replica rejoin fails readiness

- **WHEN** a removed or restarted replica attests an old epoch or lacks a current accepted
  key
- **THEN** it cannot become serving, issue, or resume a capability until it satisfies the
  current control-plane epoch

### Requirement: Authorization Session Storage Migrates And Rolls Back Explicitly

The current governance store is schema `PRAGMA user_version = 3`. This change SHALL
introduce exact schema v4 with a `governance_authorization_sessions` table containing
only stable internal session id, keyed locator digest/verifier, verifier key id,
credential generation, canonical principal, issuer family, external cell/logical-vault/
keyring ids, status, created/rotated/expiry/closed timestamps, and no raw bearer. Schema
v4 SHALL also add append-only `compiled_policy_generations`, immutable catalog
descriptors, and singleton `active_governance_tuple` carrying exactly policy generation/
fingerprint, projector version, and catalog generation as the atomic authority described
by `governance-kernel`. Migration v3→v4 SHALL be monotonic,
transactional, crash-recoverable, and SHALL conservatively expire legacy unscoped grants
or arbitrary-handle purpose/session rows that cannot prove exact new bindings. Unknown
versions above v4 SHALL remain refused. Ordinary token/receipt/policy/session openers
SHALL leave exact v3 unchanged and return `MIGRATION_REQUIRED`; only the authenticated
offline coordinator under the whole-tree/schema/replica fence may perform v3→v4 DDL/DML.

A v3 binary is not claimed compatible with a v4 sidecar. Deployment SHALL drain/quiesce
all v3 replicas before the first v4 migration and use a schema/lease rollout fence that
prevents a v3 process from joining a v4 cell. The rollout suite SHALL actually start the
old v3 binary against an isolated v4 copy and record whether each startup/read/authoring
path refuses or can run; any path that does not self-refuse SHALL make the external
rollout fence mandatory and SHALL still be barred from the live cell. Supported mixed-
replica testing refers to v4 replicas with overlapping old/new verifier keyrings, not
unproved v3/v4 concurrent writes.

Rollback SHALL use one of two explicit, tested paths: restore the pre-migration v3
sidecar backup and accept loss/revocation of all post-snapshot ephemeral authorization
state; or run an offline v4→v3 downmigration that first closes every v4 authorization
session, expires all session grants/purposes/withhold tokens bound to them, removes only
the v4 table/columns/indexes, mirrors the active tuple's exact policy-generation source bytes
to `_Governance` under the cooperative whole-tree fence, recompiles them to prove parity,
verifies every remaining v3 table against the exact v3 schema, and sets
`user_version=3`. The old binary SHALL be started only after that proof.
No in-place application downgrade may ignore v4 rows. After either rollback, a caller
must open a fresh session under the active release.

#### Scenario: Exact v3 migrates to bearer-free v4

- **WHEN** the current exact v3 fixture containing legacy handles, active grants,
  purposes, receipts, and recovery journals is migrated
- **THEN** v4 creates bound session storage without raw bearers plus one verified
  immutable compiled-policy generation, catalog descriptor, and active tuple from the
  quiesced direct-source policy/catalog, first records irreversible external enrollment,
  expires authority that cannot prove the new tuple, preserves valid v3 evidence, and
  recovers transactionally from every injected crash point

#### Scenario: Old binary is probed and fenced from new schema

- **WHEN** the actual pre-change v3 binary opens an isolated migrated v4 sidecar
- **THEN** the evidence records exact startup/read/authoring behavior and no DDL/DML is
  permitted on the fixture
- **AND** regardless of whether every path self-refuses, the rollout schema/lease fence
  prevents that binary from co-serving or writing the v4 cell and no compatibility is
  claimed

#### Scenario: Downmigration invalidates session authority

- **WHEN** operators run the offline v4→v3 rollback path against a copied fixture
- **THEN** all v4 session-bound authority is closed/expired, the remaining database matches
  the exact v3 schema and receipts, `_Governance` exactly represents the former pointed
  generation, and the actual v3 binary starts without treating an arbitrary legacy
  handle as live authority

### Requirement: Authorization State Is Bound To One Internal Session

Escalation tokens, purpose declarations, session grants, grant redemption, and session
revocation SHALL bind the stable internal authorization-session id, canonical principal,
issuer family, audience, purpose, expiry, and exact item/path/fingerprint/scope bounds
applicable to that state. The raw bearer SHALL NOT be used as a database key. State from
session A MUST NOT activate in session B, including when both sessions resolve to the
same principal and issuer. Missing or unresolved session context SHALL refuse `grant`,
session-scoped `revoke`, and `declare` rather than falling back to a standing grant or
owner identity.

#### Scenario: Same principal cannot cross sessions

- **WHEN** one principal opens sessions A and B and presents A's escalation token or
  grant while bound to B
- **THEN** redemption or activation is rejected and neither session is modified

#### Scenario: Session revoke is exact

- **WHEN** a caller revokes all authority while bound to session A
- **THEN** A's active grants are cleared and session B's grants remain unchanged

#### Scenario: Missing session fails closed

- **WHEN** a resolved principal invokes grant, session revoke, or declare without a
  verified active authorization session
- **THEN** the operation refuses without consuming a token, writing a purpose, or
  changing a grant
