## Context

PR #227 already provides one-process/one-vault hosted mode, private authenticated command and lifecycle routes, deterministic export/restore primitives, tenant-scoped HMAC transfer grants, and fail-closed lifecycle admission. The dedicated K3s platform now needs supported image-native operations for initialization, restore, credential rotation, probing, and direct browser transfer. Today it would have to import private Python functions, replace one process-snapshotted credential, or send a long-lived cell bearer through the browser.

The alpha runs Linux containers as one fixed non-root UID/GID with a read-only image filesystem and three durable writable roots: vault, state, and bounded logs. A bounded temporary transfer directory lives beneath state, not on a fourth durable volume. The control plane owns routing and grant issuance. Exomem owns cell-local binding, credential/grant verification, replay consumption, canonical vault mutation, lifecycle admission, and content-free readiness proof.

## Goals / Non-Goals

**Goals:**

- Give Kubernetes and the provisioner one normative, versioned, JSON-only, retry-safe operator CLI for init, offline restore, credential transitions, and authenticated probes.
- Bind all persistent roots to the exact opaque cell and logical vault identities, absolute layout, root kind, and expected runtime UID/GID before the server becomes ready. Release/protocol remain deployment proof fields rather than durable storage ownership.
- Make credential rotation survive restarts, support measured overlap and rollback before finalization, and never persist plaintext outside the projected Kubernetes Secret.
- Let browsers upload request bodies of at most 90 MiB and stream grant-bounded downloads directly through the transfer hostname, with no long-lived cell bearer or Vercel body relay.
- Preserve byte-identical canonical restores, define the vault-rename commit point, and recover every three-root crash boundary deterministically.
- Keep credential/JTI security state process-safe and durable without holding the canonical vault write lock across network transfer I/O.

**Non-Goals:**

- Billing, tenant routing, archive encryption/storage, backup scheduling, or Kubernetes resource reconciliation inside Exomem.
- A general remote administration API or a public grant-issuance endpoint on the cell.
- Migrating ordinary personal/self-hosted deployments into hosted mode.
- Adding manifest signatures in this change; archive authenticity is pinned by the authorized control plane's artifact reference and expected archive SHA-256.
- Adding a server-side reasoning model or changing canonical Markdown formats.

## Decisions

### 1. The checked-in operator contract is the deployment boundary

`contracts/hosted-operator-v1.json` is normative and defines one exact argv per command. Offline `init` and `restore-candidate` Jobs use `--request-file` with their fixed `/run/exomem/operator-requests/<command>.json` path. Each one-shot request is mounted read-only with Kubernetes `subPath`, so updates are intentionally unsupported: it is a root-owned regular file, has exactly one link, has no write bits, lies beneath the fixed request directory with no symlink components, and is opened once with no-follow semantics before bounded UTF-8 JSON parsing. Descriptor identity/type/owner/mode/size are checked before and after the read; replacement never changes bytes already read from that FD.

Live `credential` and `probe` operations instead use the exact `--request-file -` argv and receive one bounded request over stdin from the authenticated provisioner's non-TTY Kubernetes exec stream. They run in the existing Exomem container so credential state is current and probe's literal loopback reaches that cell, without a pod restart or a dynamically writable request mount. The provisioner invokes argv directly without a shell, closes stdin at EOF, and supplies no request through argv, environment, or a persistent file; extra bytes, an incomplete document, or more than 64 KiB are contract failures. The authorized Kubernetes exec principal and provisioner operation are the transport trust boundary. Request values never contain credentials. Both sources feed the same strict UTF-8 JSON decoder with duplicate-key/unknown-field rejection. Standard output contains exactly one JSON envelope and no prose. Standard error is empty for modeled failures. Exit status is `0` for success, `2` for contract/input failure, `3` for conflict, `4` for retryable unavailable/busy, `5` for integrity failure, and `6` for an internally redacted failure. Every valid request has a canonical UUIDv4 `request_id`; every mutating request also has an opaque `operation_id` and expected state revision where the contract names one. An argv/request failure before a canonical command or request ID can be trusted returns `command: null` and/or `request_id: null` rather than reflecting unvalidated input or inventing identity.

The contract artifact freezes commands, conditional fields, limits, success data, stable errors, exit mapping, redaction rules, and idempotency identity. Additive response fields require a declared contract revision; unknown request fields always fail. Binding format v2 is deliberately independent from CLI contract v1.

The CLI reuses hosted runtime and portability leaves but is not exposed through product MCP/REST. Ordinary CLI dispatch and dotenv behavior remain unchanged unless the first arguments are exactly `hosted ...`.

### 2. Binding v2 owns storage identity and hardens UID/GID convergence

Each vault/state/log marker persists binding version, opaque `cell_id`, opaque logical `vault_id`, normalized absolute vault/state/log roots, root kind, runtime UID, and runtime GID. UID and GID are decimal integers in `1..2147483647`; zero is forbidden for either. Release and hosted protocol are validated against the immutable image on every init/probe and returned as deployment proof, but are not persisted in root ownership markers.

Fresh roots are mode `0700`, marker/security files are `0600`, and group/other permission bits are absent from hosted-owned files and directories. Readiness verifies marker content plus the actual root and marker UID/GID/mode with no-follow stats. A non-privileged init converges only when ownership already matches. A privileged init may recursively `lchown` only a new empty root or a root with a valid matching v1 marker; it uses descriptor-relative, no-follow traversal, rejects symlinks, devices, FIFOs, sockets, and multiply-linked regular files, and bounds entries/bytes before changing ownership. A partial matching v1 migration is retryable; unowned or foreign data is never chowned. Canonical owner permissions are preserved while group/other bits are removed, directories retain owner traversal, and binding publication is fsync-plus-atomic-replace.

The initializer bootstraps the credential authority from a projected credential bundle containing exactly one requested active version. It reports success only after all three v2 markers, scaffold, security state, permissions, and deployment expectations converge.

### 3. Offline restore uses exclusive ownership and a recoverable three-root journal

Source export still requires normal cell quiescence. `restore-candidate` instead operates on a new unserved target: it acquires an exclusive lifetime lock in the target state root that the hosted server also holds for its entire process lifetime. External orchestration must stop routing and the target StatefulSet first, but the shared lock is the cell-side proof that no target server or second restore owns the roots. In-place/live restore remains forbidden.

The authorized control plane supplies an opaque internal `artifact_reference`, the exact archive SHA-256, source cell ID, and source logical vault ID. The archive format remains unsigned (`signature.value` is null); Exomem checks bytes against the out-of-band SHA-256 and then verifies manifest structure, file digests, and matching source identities. Restore requires a distinct target cell ID and the same logical vault ID. It strips/rejects all source binding, credential, lifecycle, lease, idempotency, replay, temp, and other runtime state.

One operation journal beneath the target state root binds operation ID, request digest, artifact reference, archive SHA-256, source/target identities, root binding digest, and phases `roots_bound`, `archive_prepared`, `canonical_published`, `derived_ready|derived_degraded`, and `complete`. State/log bindings are necessarily durable before publication. The only atomic publication claim is the same-filesystem rename of an unclaimed sibling vault staging directory to the absent target vault root; that rename is the canonical commit point.

Every phase and directory mutation is fsynced. On retry, a pre-commit journal resumes or safely cleans its own staging directory. If the vault exists while the journal still says `archive_prepared` (crash after rename and before journal update), exact target binding plus manifest path/byte digests prove publication and recovery advances. A mismatch is a hard integrity failure and is never overlaid. After commit, retries rebuild only disposable state, verify canonical bytes again, and finish or report a stable degraded result. Changed operation inputs conflict. The same lifetime lock excludes concurrent server start and restore through every phase.

### 4. Credentials use one dynamic, process-safe security authority

Kubernetes projects one bounded native Secret file at the fixed read-only path `/run/exomem/credentials/credentials.json` with schema `{"schema_version":1,"credentials":{"<opaque-version>":"<credential>"}}`. The volume uses AtomicWriter with `defaultMode: 0444`; no pod `fsGroup` is set, so kubelet never rewrites vault/state/log PVC GIDs or modes. The resolved file is root-owned, regular, `0444`, confined to the fixed read-only Secret mount, and the path is not caller-configurable. The AtomicWriter `credentials.json -> ..data/...` symlink topology is explicitly accepted only within this kubelet-owned mount: the server/CLI open the fixed leaf once, validate the resolved descriptor/type/owner/mode/mount/size, and read one complete generation from that descriptor. The Secret is mounted only in the single Exomem container; that container runs one fixed non-root application UID and no untrusted sidecar or user process. `0444` therefore grants no principal that would not already share the app process's credential authority—an app-UID compromise could read an app-owned `0400` file too—while preserving native atomic rotation and PVC `0700` invariants. Ephemeral debug containers are a privileged operator action outside the tenant threat boundary.

Each credential is exactly an unpadded base64url encoding of 32 uniformly random bytes. At most two versions are permitted in the bundle. The server and CLI reject duplicate keys, oversize/malformed content, unsafe AtomicWriter escape, or changed descriptor facts and never obtain credentials from command arguments, environment values, or dotenv. Extra bundle versions are not accepted until a state transition records them.

`state_root/hosted-security.sqlite` is the separate cell security authority. It is private, cell-bound, SQLite-transactional with full synchronous durability, bounded busy time, schema migration, compare-and-swap `row_revision`, and a process-safe uniqueness constraint. It stores credential versions, SHA-256 digests, phase (`stable`, `staged`, `promoted`), active/pending/preferred versions, opaque rotation ID, and bounded proof metadata; it stores no plaintext. Because credentials are full-entropy machine values, the digest is not a feasible verifier for a human password. Credential readers on both FastMCP and every private custom route resolve the same authority on each authentication decision, so a committed finalize rejects the old token on the next request without a pod restart.

The transition table is:

| Action | Preconditions | Durable result |
|---|---|---|
| `bootstrap` (inside init/restore) | no state; bundle contains exactly requested active version | `stable`, revision 1, sole active digest |
| `stage` | stable; CAS revision; bundle contains matching active plus distinct pending | `staged`, new rotation ID/revision, both accepted, proof cleared |
| authenticated `probe` | staged/promoted; selected pending authenticates; CAS identity matches | fresh proof bound to cell, pending version/digest, rotation ID, request ID, release, protocol, worker-policy digest, result, and timestamp |
| `promote` | staged; CAS revision; successful matching proof no older than 300 seconds | `promoted`, pending preferred, both accepted |
| `abort` | staged or promoted; CAS revision; original active still matches bundle | `stable` on original active, pending/proof/rotation cleared and pending rejected |
| `finalize` | promoted; CAS revision; matching proof still fresh; bundle still contains pending | `stable` on pending only, old digest/proof/rotation removed and old rejected |

Changing pending state or rotation ID invalidates proof. Crashes before a transaction commit preserve the prior accepted set; crashes after commit expose the complete new set. Retried operation IDs return the recorded proof only for an identical request digest. Finalization is irreversible through this API; rollback after finalize is a new rotation. Secret cleanup after abort/finalize may remove the now-unreferenced bundle value without changing accepted authentication.

### 5. Probe transport cannot exfiltrate a credential

`exomem hosted probe` does not accept a URL. It targets only `http://127.0.0.1:<validated-port>/private/exomem/v1/ready`; the host, scheme, path, absence of userinfo/query/fragment, and allowed port range are constructed by code. The HTTP client disables environment proxy and netrc use, never resolves DNS, follows no redirect, and uses connect/read/total bounds of 1/2/3 seconds. It accepts at most 16 KiB with exact `application/json` media type and rejects trailing/unknown fields outside the versioned bounded readiness schema.

The helper loads the selected version from the projected bundle, creates a fresh HTTP UUIDv4 request ID and random opaque principal digest, and sends the expected cell/protocol headers plus bearer. Its operator request has its own operation ID and expected credential-state revision so proof persistence is retry-safe and compare-and-swap protected. Every invocation still performs a new HTTP request and validates current readiness; operation replay never substitutes cached health. If an identical pending-proof operation already committed, the fresh result may reuse that durable proof only while security revision, rotation, credential digest, release, protocol, worker policy, and every readiness field still match. It validates success envelope, cell/vault identity, Exomem release, hosted protocol, authenticated credential version, service authentication, mutation authority, active admission, and the expected worker-policy digest, and returns the current security revision. Output never contains the token, URL, response body, root path, or tenant content. A successful pending probe records the rotation proof transactionally; an active probe reports `proof_recorded=false`; a failed or malformed response records nothing.

### 6. Direct transfer capabilities have exact routes and targets

The only public application routes are `PUT` and unauthenticated bodyless `OPTIONS` on `/public/exomem/v2/transfers/upload`, plus `GET` and unauthenticated bodyless `OPTIONS` on `/public/exomem/v2/transfers/download`, at the configured transfer hostname. Traefik maps an opaque cell prefix and strips it before the request reaches the cell. No query, body selector, cookie, `Authorization`, or service credential is accepted on either data method; OPTIONS never accepts or consumes a grant. Private v1 `/private/exomem/v1/upload` and `/download` are disabled by default. An immutable hosted compatibility deadline may enable them only until an RFC3339 instant no more than seven days after the image's signed release-build timestamp; every request checks the deadline, and an absent/expired/overlong value closes both routes. The private v1 upload body is additionally capped at 4 MiB with one active multipart parser while compatibility is enabled.

`contracts/hosted-transfer-v2.json` is the normative grant/route contract. Grant v2 is canonical JSON plus HMAC, base64url encoded, and bounded to 8192 header bytes. It binds schema/audience, signing credential version, canonical HTTPS browser origin, exact operation and HTTP method, cell ID, principal digest, UUIDv4 JTI, issue/not-before/expiry (maximum 15 minutes), and maximum bytes. A download target is one normalized vault-relative file path. An upload target carries an exact bounded metadata object plus its SHA-256 under the contract's canonical encoding: NFC basename-only `filename`, ASCII `content_type`, nullable NFC `scope/category/description`, declared file `size`, and lowercase file `sha256`; empty strings normalize to null only for nullable fields. Direct upload does not accept extracted `text`. Grant issuance and the browser compute the file hash/size before issuance. The raw PUT body is the file; metadata comes only from the verified grant, so request fields cannot diverge.

The 90 MiB (`94371840` byte) alpha ceiling applies to the entire upload request body. A grant may lower it. Signed metadata size and grant limit are validated before consumption. `Content-Length` is optional; when present it must be one canonical nonnegative decimal exactly equal to signed metadata size, while an absent length/chunked body is allowed. Conflicting or unsupported framing fails before consumption. The streamed byte count/hash is authoritative after consumption, and mismatch/overflow burns the JTI. Downloads are not subject to 90 MiB; each grant binds a maximum no greater than the configured cell storage entitlement, and the exact file size must fit before streaming. Range requests are unsupported in v2 alpha.

Validation order is fixed: route/host/method, exact origin, forbidden auth/cookie/query headers, grant header length/encoding/signature/claims/time, signed upload metadata/limit, content type and optional declared length/framing, and lifecycle transfer admission all pass before JTI consumption. Grant time validation requires `iat <= nbf < exp`, `exp-iat <= 900`, `iat <= now+30`, `nbf <= now+30`, and `now < exp`; expiration has no grace. Invalid pre-consumption requests leave the JTI unused. Once an unexpired grant is admitted and consumed, expiry is not rechecked mid-stream. The cell then atomically consumes the JTI in the security authority before reading one upload byte or opening/statting a download. Missing files, streamed overflow, hash/size mismatch, disconnect, cancellation, or any later error burn the grant. Cleanup after `exp+24h` cannot resurrect a grant because time verification runs before JTI lookup/consumption.

Lifecycle admission increments `active_transfers` before consumption and remains held until upload abort/commit or the download iterator closes. Quiesce/seal rejects new admissions and drains these public transfers exactly like private transfers. Upload network I/O writes only to bounded temporary state; final canonical publication alone enters the shared vault mutation boundary. Thus replay durability does not bypass canonical mutation serialization or hold the vault lock across a slow client.

### 7. Replay and temporary state are bounded

JTI rows are unique and transactionally consumed in `hosted-security.sqlite`, survive process restart, contain only content-free hashes/versions/timestamps, and are retained until expiry plus 24 hours. Cleanup occurs in a bounded transaction. The store rejects new grants before body/open when it reaches 10,000 unexpired/recent rows or cannot prove durable commit; it never evicts a live replay record to make room.

General process temporary files use `state_root/tmp/runtime` as `TMPDIR`; the whole directory is disposable, mode `0700`, tenant-local, and cleared with no-follow traversal while holding the lifetime lock at startup. Its application quota is 16 MiB. The only enabled alpha tempfile consumer is the private v1 compatibility parser, capped to one 4 MiB request; any optional worker such as diarization remains unready until it uses the same runtime-temp quota authority. Public v2 uploads never use global `TMPDIR`: they select `state_root/tmp/transfers-v2` explicitly. That directory is mode `0700`, permits one active public upload, and has a separate 96 MiB aggregate quota. Raw streaming uses an operation-owned exclusive `0600` temporary file, hashes/counts chunks, fsyncs before governed commit, and removes it on success, error, cancellation, or startup recovery. Startup deletes only recognized operation-owned v2 names under the lifetime lock; unknown v2 entries fail readiness. The image root and `/tmp` remain read-only/non-writable.

### 8. Public transfer responses are stable and content-safe

The transfer contract freezes upload `201 application/json` success with only operation, byte count, file digest, and committed status; it does not return the governed path or filename. Download success is `200` raw bytes with exact length, `application/octet-stream`, and deterministic attachment disposition using fixed ASCII fallback plus bounded RFC 8187 UTF-8 percent encoding of the validated basename. Error JSON has one exact envelope, stable status/code/message/retryability/new-grant mapping, no target existence oracle beyond the authorized generic not-available result, and no path/filename/body/grant/JTI. A configured-origin response receives the exact CORS/no-store policy on success and every modeled error. Post-consumption failures always require a new grant; pre-consumption busy/unavailable responses preserve the JTI for a same-grant retry while it remains unexpired.

### 9. CORS and public ingress are a narrow capability exception

The cell accepts one configured canonical HTTPS browser origin and one configured transfer host. Preflight supports only `PUT` or `GET` and the exact `X-Exomem-Transfer-Grant` plus upload `Content-Type` headers, returns no credentials/wildcard, and bounds max-age to 300 seconds. Every actual success, error, and streaming response for a syntactically valid configured origin echoes that origin, sends `Vary: Origin`, and uses `Cache-Control: private, no-store`; download exposes only bounded `Content-Type`, `Content-Length`, and `Content-Disposition` headers. Hostile/null/malformed origins get no CORS authority.

At the application and ingress layers, these two data methods and their exact OPTIONS preflights are the sole public exception. Commands, lifecycle, readiness detail, Studio, MCP, contract discovery, v1 transfers, and every unrecognized path remain private and require service/operator authentication. A v2 capability never authenticates any route other than its exact transfer operation.

## Risks / Trade-offs

- **Projected Secret and durable state can drift** → all recorded versions must match the current bundle or service authentication is unready; stage/promote can abort before finalization, and no automatic finalized downgrade exists.
- **Consume-before-bytes makes interrupted transfer non-resumable** → the control plane issues a fresh JTI; partial upload bytes are temporary and never canonical.
- **Three durable roots cannot publish atomically together** → the journal makes state/log setup recoverable and defines only vault rename as the canonical commit point.
- **UID/GID binding makes runtime identity changes explicit** → only recognized v1 migration is supported; later identity changes require a separate reviewed operation.
- **Credential version in a grant is observable** → versions are opaque non-secret identifiers and expose no tenant or provider credential.
- **Derived rebuild can delay candidate readiness** → canonical validation is mandatory; optional derived workers remain soft-failing and default-off.

## Migration Plan

1. Ship v2 binding/security/grant readers while public transfer routing remains disabled; ordinary v1 hosted cells continue to start.
2. Run `exomem hosted init` against a canary to migrate matching v1 roots, bootstrap the credential authority from the existing generated secret, and validate fixed UID/GID.
3. Restart the canary on the dynamic credential authority; require the hardened probe proof before routing.
4. Enable grant v2 issuance plus the two public transfer routes/OPTIONS for the canary. If Substrate still needs private v1, pin an immutable deadline no later than seven days after image build; otherwise leave v1 default-off.
5. Drill stage, pending-auth probe, promote, abort, restage, finalize, immediate old-token rejection, restart, quiesce during transfer, and replay rejection.
6. Drill offline restore crashes at every journal phase before enabling restore for an account.
7. Remove v1 grant issuance before the deadline; the cell closes v1 automatically at expiry. Rollback disables public ingress and returns routing to the last image/state pair; it never weakens a finalized credential.

## Open Questions

None for the private-alpha runtime contract. Provider routing, Secret rendering, and lifecycle orchestration are specified in the infrastructure change and consume these primitives.
