## ADDED Requirements

### Requirement: Generated Surfaces Inject Trusted Authorization Context

The single command registry SHALL declare which operation variants require a principal,
authorization session, and authorization-session lifecycle action. Generated MCP, REST,
Hosted, CLI, and OpenAPI surfaces SHALL expose the same public session capability
credential and lifecycle semantics while resolving canonical principal and trusted
issuer/surface family inside their adapters. The dispatcher SHALL inject one immutable
verified request context; governance leaves SHALL NOT accept a public `principal`,
`principal_scope`, issuer, or internal session-id parameter.

The registry SHALL classify every generated command, legacy leaf, finite selector
variant, retrieve/inject hook, and content-bearing writer result into the closed
credential matrix in `authorization-session-binding`: session open forbids a credential;
status/rotate/close plus session grant/revoke/declare require one; self-inspection and all
content/resolution routes accept it optionally with absent meaning standing-only and
present-invalid rejecting; owner-only/standing authoring does not derive authority from
it. No route may infer another behavior, and startup SHALL fail if a route has zero or
multiple classifications.

MCP SHALL expose one optional placeholder named exactly
`authorization_session_credential`. Bounded raw JSON-RPC/ASGI middleware SHALL extract
`params.arguments.authorization_session_credential`, remove/redact it from the envelope
and every logging/error copy, resolve trusted transport authentication, verify it, and
install immutable context before FastMCP request logging, `FunctionTool`, or Pydantic
validation. The middleware SHALL return the common credential refusal for a duplicate,
non-string, malformed, or invalid value even when ordinary arguments are malformed. It
SHALL pass a sanitized argument map to FastMCP; generated wrappers/leaves MUST NOT receive
the bearer parameter. FastMCP transport/session/request identity SHALL remain
non-authoritative.

REST and Hosted SHALL accept the credential only through the sensitive
`X-Exomem-Authorization-Session` header, separate from service/gateway `Authorization`;
body/query carriers SHALL be forbidden. Raw ASGI middleware SHALL remove/redact it before
access logging, exception copies, validation, and dispatch, then verify only after the
trusted access/gateway principal resolves. Hosted SHALL reject conflicting caller
principal headers. CLI SHALL expose only `--authorization-session-fd <fd|->`, read one
bounded bearer from a protected already-open descriptor or stdin, and clear it after
verification; literal argv and environment bearer carriers SHALL be forbidden. Generated
MCP schema, REST/Hosted OpenAPI, and CLI help SHALL advertise only their appropriate
placeholder/header/descriptor carrier.

Across all surfaces, raw extraction/redaction SHALL precede framework logging and
validation. Trusted principal resolution and capability verification SHALL then precede
ordinary coercion/validation, cache lookup/key creation, idempotency lookup, release
decision, receipt allocation, or leaf dispatch. Only an exact successful session
open/rotate response may carry the typed `issued_credential` through the non-disableable
terminal scrubber after response-schema and just-minted-value validation.

#### Scenario: Registry parity includes session lifecycle

- **WHEN** the registry and generated artifacts are inspected
- **THEN** authorization-session open, status, rotate, close, and protected resume
  semantics match across MCP, REST, Hosted, CLI, and OpenAPI

#### Scenario: Leaf cannot accept caller identity

- **WHEN** the live MCP schema, REST/OpenAPI schema, Hosted admission schema, and CLI help
  are generated
- **THEN** none exposes principal, principal scope, issuer, or internal session id as a
  caller-authoritative governance parameter

#### Scenario: Stateless MCP request id is not a session

- **WHEN** stateless MCP HTTP reconnects with a repeated or changed framework session or
  request id
- **THEN** session authority changes only when a valid server-issued capability is
  verified

#### Scenario: Unbound adapter fails closed

- **WHEN** a generated or in-process route reaches the dispatcher without the trusted
  principal/session context its registry variant requires
- **THEN** the invocation refuses before governance state or content is read and does not
  default to owner

#### Scenario: Bearer is absent from observability

- **WHEN** a request carrying a valid or invalid authorization capability is logged,
  traced, retried, rejected, or included in an idempotency calculation
- **THEN** the raw bearer is absent from all observability and persisted replay material

#### Scenario: Invalid credential wins before validation and cache

- **WHEN** a generated route receives both an invalid session credential and malformed or
  cacheable content arguments
- **THEN** it returns the common credential refusal before validation detail, cache or
  idempotency access, governance decision, receipt, or leaf effect

#### Scenario: MCP raw middleware precedes FastMCP validation

- **WHEN** an installed FastMCP stateless-HTTP request carries an invalid
  `params.arguments.authorization_session_credential` and a separately malformed tool
  argument
- **THEN** raw middleware scrubs the bearer, returns the common credential refusal, and
  FastMCP logging, `FunctionTool`, Pydantic validation, wrapper, and leaf receive no raw
  bearer or validation copy

#### Scenario: Surface carriers are distinct and protected

- **WHEN** clients inspect generated MCP, REST/Hosted, and CLI contracts
- **THEN** MCP exposes only the optional consumed placeholder, REST/Hosted expose only
  `X-Exomem-Authorization-Session` separate from `Authorization`, and CLI exposes only
  `--authorization-session-fd`; body/query/env/literal-argv alternatives refuse

#### Scenario: Actual-wire failures leave no observability copy

- **WHEN** valid, invalid, duplicate, non-string, or malformed bearers traverse installed
  MCP/FastMCP, REST, Hosted, and CLI adapters and trigger access logs, validation errors,
  exceptions, retries, traces, and debug serialization
- **THEN** an exact scan finds no bearer outside protected input/typed issuance and every
  wrapper/leaf invocation sees only trusted internal context

#### Scenario: Issuance is the only terminal scrubber exception

- **WHEN** a bearer-shaped value appears on any route or field other than the exact typed
  `issued_credential.bearer` of successful session open/rotate
- **THEN** the terminal scrubber removes it and malformed issuance refuses rather than
  weakening global bearer redaction

### Requirement: Governed Find Continuations Are Surface-Equivalent

The generated `ask_memory` and legacy `find` signatures SHALL expose the same optional
bounded string `continuation` across MCP, REST, Hosted, CLI, OpenAPI, and in-process
dispatch. The field SHALL be absent by default and SHALL NOT change never-governed
response bytes. A supplied continuation on a route without an active governed projected
runtime, or any malformed/unknown/expired/cross-binding continuation, SHALL return the
same `INVALID_CONTINUATION` application refusal. No adapter may decode the token into
caller-selectable offset, principal, session, purpose, policy, catalog, or runtime
authority.

Governed projected success SHALL use the existing envelope and MAY include exactly one
`continuation` string when another authorized page exists. Exhaustion SHALL omit the
field. The generated schemas SHALL declare that optional field without changing
ungoverned default payloads. MCP, REST, Hosted, CLI, and in-process calls with the same
trusted principal and request SHALL return the same page and continuation bytes, modulo
the already registered outer transport framing exclusions.

#### Scenario: Cross-surface continuation parity

- **WHEN** the same trusted principal requests consecutive governed pages through MCP,
  REST, Hosted, CLI, and in-process dispatch
- **THEN** each surface accepts the same bounded continuation contract and returns the
  same canonical page membership, order, exhaustion, and continuation bytes

#### Scenario: Caller cannot choose a page offset

- **WHEN** a caller edits, fabricates, replays after expiry, or moves a continuation
  across vault/principal/session/purpose/request bindings
- **THEN** the dispatcher returns `INVALID_CONTINUATION` and no decoded token field or
  registry detail reaches validation, logs, errors, or the leaf

### Requirement: Reserved Path Classification Is Registry-Total

The command registry SHALL identify every path/ref-bearing argument for every operation
and finite-selector variant, including whether it is a source, destination,
metadata-derived recovery destination, recursive root, dataset, media artifact, frame,
transfer target, or alias. Startup coverage SHALL fail when any registered public route
or selector can reach a path without a classification. The shared dispatcher SHALL apply
the canonical reserved administration-path classifier before existence checks, parsing,
counting, mutation planning, lease acquisition that exposes target state, or leaf
dispatch.

This is a boundary on Exomem commands and cooperating Exomem subsystems: untrusted
principals reach vault state only through that boundary. Direct filesystem or block-device
access as the OS vault owner is owner-equivalent and outside zero-effect and
universal-detection claims; it may disclose, corrupt, move, or delete state. The boundary
SHALL fail closed when drift is observable against retained logical, catalogue, registry,
or filesystem-identity anchors, but SHALL NOT claim to detect or reverse an unobservable
out-of-band owner action.

Classification SHALL cover one closed versioned internal-state registry. Its initial set
SHALL include `_Governance/**`, `_Consolidation/**`, the exact root-level
`Knowledge Base/` names `.governance.sqlite`, `.embeddings.sqlite`, `.clip.sqlite`,
`.lexical.sqlite`, `.graph.sqlite`, `.claims.sqlite`, `.references.sqlite`,
`.refs.sqlite`, `.freshness.sqlite`, `.deferred-index.sqlite`,
`.deferred_index.sqlite`, `.media-jobs.sqlite`, `.media_jobs.sqlite`,
`.idempotency.sqlite`, `.idempotency.json`, `.idempotency.jsonl`,
`.media-jobs.json`, `.deferred-index.json`, `.voice_profiles.json`,
`.media-worker.lock`, `.graph-sync.json`, `.graph-sync-floor.json`,
`.graph-commit-receipts/**`, and `.review-state.json`; current review-state temps matching
exactly `..review-state.json.[a-z0-9_]{8}.tmp`; lexical rebuild state matching exactly
`.lexical.sqlite.rebuild-[0-9a-f]{32}.tmp` plus that temp database's `-wal`, `-shm`, and
`-journal` siblings; lexical quarantine members matching exactly
`.lexical.sqlite.quarantine-[0-9a-f]{32}`,
`.lexical.sqlite-wal.quarantine-[0-9a-f]{32}`, and
`.lexical.sqlite-shm.quarantine-[0-9a-f]{32}`; plus
`Knowledge Base/.authorization-projections/**`. Every ordinary SQLite
entry SHALL include its exact `-wal`, `-shm`, and `-journal` family and the graph entry
its bounded registered rebuild-temp family. This closed set covers governance/session
authority, journals, raw lexical/vector/CLIP/reference/graph state, immutable projected
indexes, and active catalog descriptors. A new internal store/index/temp/lock cannot run
until it is registered and registry-total tests pass.
For both `/**` descriptors the root directory itself and every descendant SHALL be
reserved, including unknown/future child names; recognizing only today's receipt format
is insufficient for the ordinary-operation boundary.

The initial descriptor set SHALL be generated/audited against every current private-
state owning module and path factory, not copied from the hosted-portability list. The
audit SHALL enumerate primary files, directories, transactional siblings, owner-created
temps/quarantines/receipts, and runtime physical identities for governance, lexical,
vector, CLIP, graph/handoff, refs/claims, review, deferred/media/idempotency, voice, and
projection owners. Every owner-produced form SHALL map to exactly one descriptor; an
unmapped or multiply mapped private path SHALL fail startup and registry tests before
the owner may create/open it. Portability/export rules SHALL consume this security
registry or be checked against it, never define a smaller authority boundary.

Logical names are reserved before they exist. At protected acquisition, a stable
pre-existing symlink, reparse point, hard link, or physical alias to a reserved family
member SHALL be refused. Each owning subsystem SHALL retain and publish stable identities
of open primary/WAL/SHM/journal/temp/index files under the leaf coordination primitive.
SQLite primary/WAL/SHM identities SHALL be published before cooperative coordination is
released, not before filesystem reachability. Secure resolution SHALL compare retained
identities where `realpath` alone cannot expose an alias and SHALL reject multiply linked
or ambiguous internal-state files. It SHALL check both ends of move/copy/replace, trash
source and every possible restore destination, and every child of a recursive operation.
An observable anchor discrepancy or non-canonical resolution SHALL fail closed. A private
owning-subsystem authority MAY pass the dispatcher check; no serialized argument,
generic alias, surface, owner/L6 decision, non-Markdown classification, or Tier-2 flag
may do so.

Logical classification SHALL route/refuse early but SHALL NOT authorize a later pathname
reopen. Every generic filesystem leaf SHALL execute through a descriptor-bound,
handle-relative reserved-path transaction: open the vault root and parents without
following links, hold them through the leaf operation, classify stable volume/device and
file identities, and use relative create/read/write/rename/link/unlink primitives. POSIX
SHALL use `openat2` beneath/no-symlink/no-magic-link constraints or an equivalent
iterative dirfd/`openat`/`O_NOFOLLOW` implementation. Windows SHALL use `NtCreateFile`
with `RootDirectory` and a relative name plus `NtSetInformationFile` rename/disposition
semantics, reparse-aware handles, and final volume/file identity; the route SHALL remain
disabled unless a runtime actual-filesystem capability probe proves those exact relative
handle operations, no-follow/reparse behaviour, and final identity checks. Failure or
absence of that probe SHALL disable the route and return the registered refusal without a
fallback. A windows-latest required CI gate SHALL exercise NTFS junction, reparse,
hard-link, 8.3, rename/disposition, and fallback-disable fixtures, and SHALL be wired
into combined release verification. Same-device rename, trash, and recovery SHALL hold
both parent handles and run under cooperative coordination; cross-device move, trash,
and recovery SHALL refuse. A copy SHALL read the held source and publish atomically only
at the held destination; it SHALL NOT claim source-and-destination atomicity. Recursive
and multi-entry power-loss handling SHALL use a saga and recovery, not cross-file or
recursive atomicity. Parent swaps, rename/link races, hard links, reparse points, and
bind aliases SHALL be checked at the kernel read/mutation operation against retained
anchors, not by check-then-`realpath`. A platform without equivalent primitives SHALL
disable the affected generic route.

Enumeration and retrieval routes—including list/walk/browse/search/find/get/fetch,
dataset/Records/media/frame, download/export/transfer, graph/provenance, audit/repair,
trash/recovery, and recursive packaging—SHALL remove registered internal state before
existence, candidate, count, ordering, or manifest computation. Generic mutation routes
SHALL refuse a reserved target before touching that target. Multi-entry mutation and
recovery SHALL use an ordered, descriptor-bound preflight and saga: each entry is
revalidated immediately before its durable effect, each completed entry records durable
receipt/journal state, and later refusal or interruption is handled by recorded recovery,
not by treating all entries as one transaction. A private state file MUST NOT enter the
governance membership evaluator as an ordinary non-Markdown artifact at L6.

#### Scenario: Case Unicode and separator variants remain reserved

- **WHEN** a route spells a reserved component with case variants, NFKC-equivalent
  Unicode, backslashes, mixed separators, or a knowledge-base-prefix variant
- **THEN** every generated surface classifies it as the same reserved root

#### Scenario: Stable aliases and symlinks cannot bypass the root

- **WHEN** protected acquisition finds a canonical ref, managed alias, short-name alias,
  or stable pre-existing symlink resolving into a reserved tree without spelling its name
  in the public input
- **THEN** the dispatcher and secure leaf resolution both refuse or hide the target within
  the command boundary

#### Scenario: Filesystem identity alias cannot bypass the root

- **WHEN** protected acquisition finds a stable pre-existing hard link, bind-style alias,
  or multiply linked file outside the reserved spelling that refers to
  administration-tree state
- **THEN** retained filesystem-identity checks refuse it as reserved or fail closed on an
  observable anchor discrepancy

#### Scenario: Move checks source and destination

- **WHEN** either end of a move, copy, replace, or transfer resolves inside a reserved
  tree
- **THEN** the entire operation refuses before any source or destination mutation

#### Scenario: Recovery checks explicit implicit and recursive destinations

- **WHEN** a recover operation uses a trash path, explicit restore path, original path
  from metadata, alias, or recursive child that resolves to a reserved tree
- **THEN** the ordered preflight refuses before that entry is restored; entries already
  durably completed by the recovery saga are reconciled from their receipt/journal state,
  and recovery remains per-entry rather than transactional across its full set

#### Scenario: Dataset and media selectors are covered

- **WHEN** query/dataset, Records, process/read media, video-frame, upload/download, and
  multiplexed management variants are enumerated
- **THEN** every path/ref selector is registry-classified and none can reach a reserved
  tree through an unclassified branch

#### Scenario: Private activation family is never ordinary L6

- **WHEN** `.governance.sqlite`, any exact WAL/SHM/journal sibling, or a retained/
  published physical alias is targeted through list/walk/search/get/download/dataset/
  export/transfer/recovery at owner/L6
- **THEN** it is structurally absent before membership/projection and no byte, row,
  count, name, hash, timing signal, or existence bit is returned

#### Scenario: Raw and projected index families are equally reserved

- **WHEN** a generic route targets `.embeddings.sqlite`, `.clip.sqlite`, `.lexical.sqlite`,
  `.graph.sqlite`, `.refs.sqlite`, `.authorization-projections/**`, a registered legacy
  spelling, journal sibling, rebuild temp, or retained/published physical alias
- **THEN** reads/enumeration hide it and generic mutation refuses independently of
  whether the file currently exists

#### Scenario: Graph handoff and review-state families are reserved

- **WHEN** a route targets `.graph-sync.json`, `.graph-sync-floor.json`, the
  `.graph-commit-receipts/` root or any descendant, `.review-state.json`, or an exact current
  `..review-state.json.<8-char-token>.tmp`, before or after owner creation
- **THEN** list/search/get/download/export/dataset/recovery treats it as absent and every
  generic create/move/delete/recover refuses without revealing stable existence

#### Scenario: Lexical rebuild and quarantine generations are reserved

- **WHEN** a route targets an exact `.lexical.sqlite.rebuild-<32-lowerhex>.tmp` family or
  the main/WAL/SHM `.quarantine-<same-32-lowerhex>` group during publish/rollback
- **THEN** dispatcher and held-leaf identity checks hide/refuse every member, including a
  pre-create spelling and an alias raced between quarantine and restore

#### Scenario: Every private-state owner is inventoried

- **WHEN** current owner path factories and write/temp/quarantine/receipt paths are
  enumerated independently of hosted portability
- **THEN** each maps to exactly one internal-state descriptor and a missing/duplicate
  mapping fails before startup or owner creation

#### Scenario: WAL creation and checkpoint race stay reserved

- **WHEN** a cooperating internal subsystem creates, checkpoints, renames, or removes a
  WAL/SHM/journal/staged-index file while a generic read, move, link, delete, recovery,
  or export observes the same retained logical or physical identity
- **THEN** shared held-leaf coordination classifies the identity as internal or fails the
  generic operation closed; SQLite identities are published before coordination release

#### Scenario: Internal-state registry is closed

- **WHEN** code introduces an internal database, journal, lock, temp pattern, raw lane,
  projected lane, graph, or catalog file without a descriptor
- **THEN** startup/schema coverage fails before the owning subsystem or generic command
  can open it

#### Scenario: New path-bearing command fails until classified

- **WHEN** a command, action, operation, mode, or alias with a new path/ref field is added
  to the registry
- **THEN** registry/startup coverage fails until its role and reserved-path behavior are
  declared

#### Scenario: Parent swap cannot cross the reserved boundary

- **WHEN** a stable pre-existing or anchor-observable parent swap resolves to a symlink,
  junction, reparse point, or bind mount into a reserved tree before the leaf
- **THEN** the held-handle operation refuses before the protected acquisition or mutation

#### Scenario: Rename and hard-link races cannot bypass identity

- **WHEN** a source/destination rename, hard link, or alias exchange produces an
  observable retained-anchor mismatch after logical classification
- **THEN** the leaf refuses; no portable final-component filesystem guarantee is assumed
  beyond the platform primitives named above

#### Scenario: Unsupported platform primitives fail closed

- **WHEN** a platform cannot provide no-follow handle-relative traversal and mutation
  through the leaf for a possibly reserved target
- **THEN** the generic route returns the content-free reserved-path refusal and MUST NOT
  fall back to check-then-path-use

#### Scenario: Cross-device and multi-entry operations have bounded semantics

- **WHEN** a generic move, trash, or recovery crosses devices, or recursive/multi-entry
  work is interrupted by power loss
- **THEN** the cross-device move/trash/recovery refuses, while copy publishes only its
  destination atomically and recursive/multi-entry work uses ordered preflight, per-entry
  durable effects, and recorded saga recovery rather than claiming all-or-none atomicity

### Requirement: Reserved Path Outcomes Are Surface-Consistent

MCP, REST, Hosted, and CLI SHALL produce the shared content-free outcome for the same
reserved-path request. Ordinary read/enumeration operations SHALL use the same missing
contract as structural absence. Generic mutations SHALL use one stable reserved-path
code and remediation naming only the owning command, without probing or reporting
whether the requested reserved target exists. The caller-supplied spelling MAY be echoed
only where the existing caller-input error contract permits it; no resolved alias,
canonical administration path, child count, or metadata-derived destination may be
returned.

#### Scenario: Read parity hides existence

- **WHEN** the exact same ordinary reserved-path read is issued through MCP, REST,
  Hosted, and CLI, first with the target present and then absent
- **THEN** all surface envelopes match their missing-path contract and reveal no
  existence difference

#### Scenario: Mutation parity names only the owning command

- **WHEN** the exact same generic reserved-path mutation is issued on each surface
- **THEN** each returns the shared stable code/remediation, performs no write, and emits
  no resolved path or tree metadata
