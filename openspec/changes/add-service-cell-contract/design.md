## Context

A **cell** is one Exomem process with one vault. A **service** is an outside program that uses a cell as its memory and knowledge layer: a consumer product, or an enterprise system. An **agent** is a model-driven client that uses the MCP tools. A **subject** is one end user of a service, such as a member of a tenant's audience.

The first service is a multi-tenant question-answering product with one cell for each tenant. It uses the passage-evidence and inventory work, which is in review. With that work it does the following:

1. It checks the cell's identity through content-free discovery (`GET /.well-known/exomem-cell`) before it sends a credential.
2. It reserves a capture attempt, captures one record as a Source with declared passages, and settles the attempt.
3. It reads captured-Source inventory to learn what the cell holds and whether it is indexed.
4. It ranks with passage search. It reloads its own records by passage key and checks tenant, revision, key and hash before anything reaches a prompt. No cell text enters a prompt.

These facts of the current code shape the design:

- **The service uses the agent door.** It sends JSON-RPC to the MCP endpoint and parses agent replies. For a capture it reads the leaf result from the reply's diagnostics. The in-review work gives four service commands the `service` listing tier. The door's tool schema omits them, but the server accepts them.
- **A service credential is the owner.** On a Cloud cell the cell bearer resolves to the owner audience. Passage search, inventory and erasure require whole-namespace content authority, which only an unrestricted owner holds.
- **The Cloud boundary covers one path.** `CloudOriginMiddleware` checks the gateway proof only on its configured MCP path (`src/exomem/cloud_origin.py:317`, `:333`). Custom Starlette routes sit outside FastMCP's bearer check, so `/upload` checks the bearer itself (`src/exomem/server_transfer.py:772-777`).
- **The gateway forwards one path.** The Cloud gateway forwards `EXOMEM_CLOUD_MCP_PATH` to a cell (`infra/helm/platform/templates/cloud-gateway.yaml:141`). The gateway image is released from a separate repository.
- **Containment is moving cells to proof-only requests.** `contain-cloud-controller-compromise` D1 makes a valid gateway proof the only accepted request credential and retires the fleet bearer. Its D2 pins every trust anchor that a cell reads in the admission policy, out of the controller's reach.
- **The personal REST facade exists, but not on cells.** `/api/<tool>` renders registry commands over HTTP, with an OpenAPI document generated on request (`src/exomem/server_rest.py:545-650`). It serves local installs with the owner's REST key. Most of its result schemas are untyped, and it has no version.
- **One status map exists.** `cli_ops.http_status_for` (`src/exomem/cli_ops.py:539`) maps an error code to an HTTP status for the REST facade, edge ingress and the transfer routes.
- **The governance kernel already decides disclosure per audience.** Scopes, standing rules, grants, purpose and organisation caps map an item to a ceiling from L0 to L6. A scope can deny every audience that no rule names. A residual scope selects what no other scope selects. The owner is exempt from that default-deny.
- **Connector ceilings exist.** A host-owned configuration gives authenticated OAuth clients of one owner different denied and writable scope sets. A shared cell credential receives the restricted default.
- **The call ledger and the request budget cover MCP calls only.** Disclosure receipts record governed egress in a hash chain.
- **The single store is in flight.** `make-vault-store-canonical` makes one SQLite store canonical, with Markdown as a projection. Its task 6.2 lets a Cloud cell run without per-write projection.

A service uses a cell in one of two disclosure modes. The design treats them differently:

- **Index-only evidence.** The cell returns keys, hashes and scores. The service reloads and authorizes its own records. A cell defect can cost ranking quality, but it cannot disclose text. Today's retrieval uses this mode.
- **Cell-authoritative content.** The cell returns text that the service shows or puts in a prompt: tenant memory, member-private memory, member feedback. Here the cell alone decides who may read each item.

## Goals / Non-Goals

**Goals:**

- A service integrates through one deterministic, versioned, typed contract. It needs no knowledge of agent replies.
- Exomem keeps one implementation of each operation and one HTTP renderer. Agents and services reach the same leaves.
- Isolation and governance stay inside Exomem. A caller defect cannot widen what a principal may read.
- Each step ships default-off behind host configuration and has its own proof.
- An enterprise integrator can integrate from the published document alone.

**Non-Goals:**

- Changes to the agent MCP tools or their replies.
- A hand-written SDK in any language.
- The service API on personal local installs. They keep the REST facade. Hosted and self-hosted enterprise cells are an open choice below.
- The store-only cell profile. `make-vault-store-canonical` owns it (D10).
- Turning on any stage for a tenant. Each consumer's activation follows its own approval.

## Directions considered

### A. The agent door is the SDK

Keep MCP as the only door, with its newer features. Each service tool declares an `outputSchema` and returns `structuredContent`. The server runs streamable HTTP in stateless mode with JSON responses, so a call needs no long-lived session. Service tool names carry a contract version.

- **Integrator, day one:**
  1. Receive the cell address and a credential from the operator.
  2. Connect an MCP client library and run the initialize step.
  3. Call `tools/call` with the documented service tool names, and validate `structuredContent` against each tool's output schema.
- **First consumer changes:** it reads structured content instead of diagnostics. It can drop its session pool, but it keeps an MCP client.
- **Verdict: rejected, narrowly.** A adds no second door, and output schemas would help agents too. B still wins on four points:
  1. **Versions.** MCP has no contract version. Versioned tool names put service versions into the agent tool namespace, or hide tools that the server still accepts.
  2. **Refusals.** An MCP tool error is a result flagged `isError` with free content. No protocol field carries a closed code, a status class or a retry hint that gateways and retry libraries act on.
  3. **Clients.** OpenAPI has maintained client generators in many languages. MCP output schemas have no toolchain of that maturity.
  4. **Review.** An enterprise security review knows HTTP APIs with OpenAPI documents. MCP adds a JSON-RPC envelope and an initialize step, even in stateless mode.

  Output schemas for agent tools remain worth adding. That is separate work.

### B. One HTTP renderer with a versioned service profile, and a generated client

Evolve the personal REST facade into one HTTP renderer of the command registry. Its route binding, body decoding, error envelope, status map and OpenAPI generation serve two profiles:

- The **personal profile** keeps `/api/<tool>` unchanged for local installs.
- The **service profile** serves versioned, typed service operations on cells, behind the full MCP boundary (D13).

- **Integrator, day one:**
  1. Receive the cell address and a credential from the operator.
  2. Read discovery and compare the cell identity, the served versions, the served operations and the document digest.
  3. Download the document and generate a client with a maintained OpenAPI generator.
  4. Call `POST /service/<version>/sources.reserve_capture` and the other operations. Handle refusals by code and HTTP status class.
- **First consumer changes:** it replaces its MCP transport with the generated client and a pooled HTTP client. It maps refusal codes to its own outcomes. Its convergence, verification, score map and readiness logic stay, because the leaves and their semantics do not change.
- **Verdict: recommended.** It uses mature tooling everywhere, keeps one implementation and one HTTP renderer, and gives the service contract its own version line.

Two shapes were weighed against one renderer with two profiles:

- **Make `/api/<tool>` the service contract.** It is unversioned and renders every agent command, and it serves local installs with a local owner key. Making it the contract would break its current users and put agent commands under the service version rules.
- **Write a second HTTP rendering.** Two renderings of one registry would drift in error mapping and document generation.

### C. An embedded library

Ship Exomem as a library that the service opens in its own process over a per-tenant store file. The package API is the SDK.

- **Integrator, day one:**
  1. Install a pinned Exomem release.
  2. Provision store files and embedding models on its own hosts.
  3. Call library functions in its own process.
- **First consumer changes:** it removes the network hop, discovery and credentials. It takes over storage, backup, model serving and erasure of the files on its hosts.
- **Verdict: rejected.** Governance would run in the caller's process, so a caller defect could open another tenant's file. Erasure proofs and operator-access limits would depend on the caller's hosts. It serves one language only.

## Sources

Three rows of the "Pick by problem" table in the engineering primary-source roster shape this design:

- "Untrusted content can influence an outbound action" (Lethal trifecta; CaMeL; How we contain Claude). The first service never lets cell text reach a prompt, and D8 keeps per-person content out of cells until the cell enforces its readers.
- "Answer claims need references to source material" (Citations docs; separate pointer validity from claim support). The first service verifies each hit against its own rows before it cites one.
- "The model keeps misusing a tool/MCP surface" (Writing effective tools). The agent door stays shaped for agents, and the service contract is a separate profile (D1).

## Decisions

### D1. Services use a versioned profile of one HTTP renderer

A **service operation** names one registered command. It fixes or narrows that command's arguments and declares a request model, a result model and its refusal codes. It resolves to its command before authorization, as MCP renderings do. It never adds behaviour that the leaf lacks.

The cell serves `POST /service/<version>/<operation>` on its existing listener when the host setting `EXOMEM_SERVICE_API` lists that version. Every operation is POST with a JSON body, so no query text, content or identifier appears in a URL or a proxy log.

A **version** is named by the month in which it is cut, such as `2026-12`. A month name cannot be confused with the Cloud cell resource policy `service-v1`, which is unrelated.

The first version renders the commands that the first service already calls:

| Operation | Command |
|---|---|
| `sources.reserve_capture` | Source capture reservation |
| `sources.capture` | `capture_source`, with declared passages and an attempt required |
| `sources.settle_capture` | Source capture settlement |
| `sources.inventory` | captured-Source inventory |
| `sources.erasure_policy` | Source erasure policy |
| `sources.erase` | captured-Source erasure, when `erase-captured-source` ships |
| `passages.search` | `search_memory` at the passage result level |

The service profile and the personal profile share one renderer, extracted from `server_rest.py`. They differ in the operation set, the path, the authentication boundary, the version rules and the status overlay (D4). The personal profile's paths, replies, statuses and document stay as they are. FastAPI was the closest outside option. It would add a second routing and validation layer beside the registry binding.

### D2. One fixed document for each version is the SDK

Each release holds one OpenAPI 3.1 document for each version that it serves. The document is generated from the registry, and its generated copy is committed in the repository, so a contract change is visible in review. A test keeps the committed copy equal to the generation.

- **One document per release.** Every cell of one release serves the same bytes for one version. A cell that does not serve an operation of the document refuses it with `OPERATION_UNAVAILABLE`. Discovery lists the operations that the cell serves. A per-cell document was the other option. It would give each cell its own digest and its own generated client.
- **Digest.** The advertised digest is the SHA-256 of the exact bytes that the cell serves, which are the committed bytes. Within a version the digest changes only when a release adds to the document.
- **Path.** The cell serves the document at `GET /.well-known/exomem-cell/service-api/<version>.json`, outside the POST operation namespace. Like discovery, it needs no credential and holds no tenant content.
- **Clients.** Consumers generate clients with maintained tools, such as `openapi-python-client` or `openapi-typescript`. Exomem writes and maintains no client by hand. Whether Exomem also publishes generated client packages is an open choice.
- **Repeats.** Each operation states in the document whether a repeat of the same request is safe, and which identifier makes it safe. The capture protocol already supplies these identifiers: the attempt handle and the erasure operation identifier. The service API adds no generic replay layer, because the capture protocol bypasses generic replay by design.

### D3. A version changes only by addition

Within one version a release may add operations, optional request fields, result fields, error codes and result-state values.
These changes need a new version: a removal, a rename, a change of type or meaning, a new required request field, or a refusal of an accepted value.
The cell serves both versions until the earlier version's retirement date, which discovery advertises.

The schema shapes follow from what generated clients tolerate:

- **Results are open.** A result schema allows fields that it does not list, so a client from an earlier document ignores an added field.
- **Every result field is required.** An absent value is an explicit `null`. A removed field is then a removed required property, which the CI check rates as breaking. This also keeps an unknown value visible instead of omitted.
- **Requests are closed.** A request schema refuses fields that it does not list. A new request field is optional.
- **Codes and states are strings.** `code` and every result-state field have type `string`. The document lists their known values in the description and in an `x-exomem-known-values` extension. Generators such as `openapi-python-client` turn an `enum` into a type that raises on an unknown value, so an added code would break earlier clients. The other option was to declare every enum addition breaking. Codes and states grow with the leaves, so that option would force a new version for routine additions.
- **Request enums stay enums.** A client from an earlier document never sends a value that a later release adds.

CI runs `oasdiff breaking` between the base branch's committed document and the new one, with `--fail-on ERR`. oasdiff rates a removed required response property, a new required request property and a narrowed request value as `ERR`. Task 1.9 proves the first case.

### D4. Refusals carry codes in fixed status classes from one map and a service overlay

Every refusal carries one string code. Its HTTP status comes from `cli_ops.http_status_for`, the map that the REST facade, edge ingress and transfer routes already use.
That map sends most codes to 400, including authority refusals such as `GOVERNANCE_AUTHORITY_REQUIRED`. The personal profile keeps the map unchanged.
The service profile reads the same map through a service overlay. The overlay adds three classes: 401 for credential and proof refusals, 403 for authority refusals and 500 for unexpected failures.
The overlay also sends every busy refusal to 503 with `Retry-After`. The shared map sends `MUTATION_BUSY` and `MUTATION_WARMING` to 409, and the personal profile keeps that status.
A service client needs one status class for "busy, retry later", with a retry hint. A 409 means a conflict that the caller must resolve.
The overlay lists its codes in `cli_ops`, next to the shared map, so one module holds every code-to-status rule. For any other code, the overlay returns the shared map's status.
The other option was to change the shared map and declare the `/api` change. The personal facade has local clients and no version, so nothing would warn them of a new status. The spec promises them unchanged replies.

The service profile uses these classes:

| Status | Class | Client action |
|---|---|---|
| 400 | invalid request | change the request; the error names the field |
| 401 | credential or proof refused | stop; check the credential and the route |
| 403 | authority refused | stop; the principal lacks authority |
| 404 | resource not found, or operation unavailable | for a resource, check the identifier; for an operation, read discovery |
| 409 | conflict or stale precondition that the caller resolves | read current state, then decide |
| 500 | unexpected failure | treat the outcome as unknown; read state, or repeat with the same identifier when the operation states it is safe |
| 503 | busy or unavailable, including a busy mutation | retry after the hint |

The service renderer refuses some requests before any leaf runs. Their codes are the same for every operation:

| Refusal | Code | Status |
|---|---|---|
| no valid gateway proof under enforcement | `CLOUD_PROOF_REQUIRED` | 401 |
| missing or invalid credential | `UNAUTHORIZED` | 401 |
| operation or version not served by this cell | `OPERATION_UNAVAILABLE` | 404 |
| request outside its schema | `INVALID_REQUEST`, with the field | 400 |
| busy model or mutation capacity | the leaf's existing busy code | 503 |

A leaf code that the operation does not declare still reaches the client with its class status, because codes are strings. The cell counts each such code by operation. The conformance run fails on any undeclared code.
A busy cell refuses at once with a retry hint and the `Retry-After` header. It does not queue.
An uncertain outcome, such as a pending settlement, is a typed result state, not an error.
A message can accompany a code for people. A client acts on the code alone.
The status classes are protocol tokens that this specification fixes. Leaf codes come from the leaves. The renderer owns only the pre-leaf codes in the table above.
The shared request boundary also refuses before the leaf, for example on a read-only cell, a quiescing cell or a held writer lease. Those refusals keep the codes that the boundary raises on every door.

### D5. Stage 0 is a requirement of the passage-evidence delivery

Stage 0 is the requirement "Enrolled Sources stay off shared generated pages" of the `captured-source-inventory` capability. The passage-evidence delivery (#2852) adds it, with its code.
A Source captured under erasure custody, through an issued reservation or an erasure-enabled identity system, gets no line on the top index, the activity log or the Sources index. The captured-Source inventory is its listing.
The same delivery amends `erase-captured-source` to match. As a consequence of the requirement, erasing an enrolled Source leaves no line on those pages to remove.
This change does not restate the requirement, and it merges after #2852. Stage 1 does not depend on Stage 0. The service API renders capture as the leaf behaves.

### D6. Service authority has three layers (Stage 2)

A **service principal** is a non-owner identity that the control plane registers for one product. On a Cloud cell the gateway authenticates the product's service credential and signs the principal id into the origin proof. The cell trusts only the gateway's pinned key, so it holds no service verifier registry.
A verifier registry that the controller renders into a cell would let a compromised controller mint principals. `contain-cloud-controller-compromise` exists to stop that attack. Other cell modes choose their verifier in their own change.
The control plane declares who a principal is. The cell owner's policy decides what it may do (D11).

1. **Principal.** The proof resolves to a non-owner audience in the kernel's identity space, under a reserved `service:` prefix. OAuth audiences are digests of issuer and subject, so no OAuth login produces that prefix. The prefix stays authorable: owner policy can name a service audience in standing rules and grants.
2. **Custody.** The owner's grant names the identity systems that a principal holds. A principal has full authority over the Sources of those systems: capture, settlement, inventory, passage search and erasure. This replaces whole-namespace authority for that principal. Two principals cannot hold one system. The host erasure policy still enables erasure per system.
3. **Kernel.** Every other item reaches the principal only through the kernel's decision for its audience. A service audience that no standing rule names receives no disclosure, also for an item outside every scope. This default is an evaluator rule, not a policy document, so a policy edit cannot remove it.

S2 writes these layers as a `governance-kernel` delta. It turns them on in this order:

1. The control plane registers the product's service principal. Without a committed grant, the principal reaches nothing.
2. The owner credential moves to the tenant, and the product's owner-equivalent credential stops working. Before this step the product could commit its own grant as the owner.
3. The tenant commits the product's grant. The product's onboarding runs steps 2 and 3 in one session, so the product loses access only briefly.
4. The product switches to its service principal.

When consumers have moved to their own principals, the agent door refuses service operations. The connector boundary stays as it is for OAuth clients.

### D7. Exomem binds subjects from verified assertions (Stage 3)

A service request MAY carry a **subject assertion**: a short-lived JWS signed with a key that the control plane registered for the principal, with the cell as audience.
On a Cloud cell the gateway verifies the assertion and signs the subject into the origin proof. The cell derives the subject's audience id with the kernel's existing normalization of issuer and subject, using the principal as issuer. Two services' subjects therefore never share an id.

- An item written in a subject-bound request belongs to that subject's partition. Exomem sets the partition. No request field can set or change it.
- The kernel discloses a partitioned item only to a request bound to the same subject, within the principal's own ceiling.
- A request without an assertion sees no partitioned item.
- Filters, tags and references in a request only narrow this result.

Exomem cannot detect a service that asserts the wrong subject. The service mints assertions only at its authentication edge and carries them in request context, so ordinary business code never chooses a subject.
The partition key needs a page fact. In store mode it belongs with the page admission facts of `make-vault-store-canonical` (D13 of that change). The Stage 3 change decides whether file mode gets an equivalent or Stage 3 requires store mode.

### D8. Per-person content waits for Stage 3

No service stores cell-authoritative content about one person until Stage 3 ships on that cell. That covers member-private memory and member feedback.
Until then a service credential is owner-equivalent inside its cell. Isolation between tenants holds today, because each tenant has its own cell and credential.
Index-only evidence needs no partition, because the service authorizes its own records.

### D9. The audit export reuses existing chains (Stage 4)

The export is one service operation over the call ledger and disclosure receipts. It returns content-free rows for a time window in pages, with the chain anchors at both ends, so a client can verify continuity.
A principal receives its own calls; the owner receives all. A gap or truncation is reported as incomplete, never hidden.
Stage 1 already writes a call-ledger row for every service call, so the export starts with a complete record.

### D10. The store-only cell belongs to the single-store change

A cell without Markdown projection is `make-vault-store-canonical` task 6.2. This change adds no projection rule and depends on that task only through D7's storage.

### D11. The tenant owns the cell, and the product is a service principal (Stage 2)

A cell that a product provisions for a tenant belongs to the tenant. Ownership is not hosting: the product may provision, host and pay for the cell, and the tenant still owns it.

- The product reaches the cell only as a service principal (D6). The tenant's policy grants its authority. The grant takes effect when the owner commits it through the kernel's propose and commit flow.
- The tenant holds the owner credential and the owner sign-in. From S2 the product holds only its service credential. Until S2 the product holds an owner-equivalent credential, as today, and S2 ends that state (D6, step 2).
- The control plane keeps the paying account apart from the owner. The product's account may pay for the cell, but owner sign-in binds to the tenant's login.
- The tenant may connect their own agents to the same cell through MCP, under their own principal. The kernel keeps the two apart. The product reaches only its custody and what the tenant grants it. The tenant's agents reach what the owner's policy and connector ceilings allow, except member partitions (D12).
- The product must tolerate three tenant decisions: a narrowed grant, a revoked grant and the tenant leaving with their cell.
  A narrowed or revoked grant reaches the product as a 403 authority refusal on its next request, not as an outage.
  When the tenant leaves, the product's credential stops working. The product keeps its own records and loses access to the cell.
- A host with machine access can bypass any in-process rule. Against such a host, ownership holds only as far as the cell's operator-access limits.

### D12. A member's partition is private from the owner (Stage 3)

- The owner cannot read a member's partition by default. This narrows the kernel's owner exemption: for a partitioned item, the owner is an audience like any other. The S3 change writes that delta to `governance-kernel`.
- The owner may read one member's partition only under an explicit kernel grant that names the partition, the purpose and an expiry. Every read under that grant writes a disclosure receipt.
- Without such a grant, the owner sees aggregates across partitions only, never one member's items. The S3 change sets the aggregate forms and the smallest group that an aggregate may describe.
- The owner's agents and connectors inherit this limit, because they act under the owner's audience or a narrower one.
- An owner export that produces readable files leaves member partitions out. A cell transfer to another host keeps them under the same protection.
- The product mints subject assertions, so it can bind any of its members and read that member's partition. Partition privacy holds against the owner, the owner's agents and other principals. It does not hold against the product that the members use, which is their identity provider.
- Two rules follow from this decision. A member reads their own partition through a request bound to their subject (D7). A member's erasure request erases their partition (S5), and the owner can erase a partition without reading it.

### D13. Service requests pass the full MCP boundary (Stage 1)

A custom Starlette route skips FastMCP's bearer check, and `CloudOriginMiddleware` checks proofs only on the MCP path. A service route mounted as a plain custom route would skip both checks. So the service profile runs inside the same boundary as `/mcp`, not only the same leaf:

1. The bearer verification that sets the request's user.
2. The origin middleware's proof check and containment's verdict. S1 extends its path match from the MCP path to the service prefix.
3. The connector boundary, the request budget and the call ledger.

`contain-cloud-controller-compromise` interacts with this in three ways:

- **Proof.** Its D1 migration covers the service prefix. In report mode the cell logs which credential each service request used. Under enforcement the cell refuses a valid bearer without a valid proof with `CLOUD_PROOF_REQUIRED`, exactly as on `/mcp`. After the bearer retires, a service reaches a Cloud cell only through the gateway, which is why D6 puts service principals into the proof.
- **Anchors.** The service API adds no trust anchor. `EXOMEM_SERVICE_API` turns routes on and grants no authority. The closed pod template of its D3 must allowlist the setting's name.
- **Revocation.** The gateway refuses a revoked login session and signs no proof for it. A proof that it already signed expires within its short lifetime. The cell then refuses the request as it does on `/mcp`.

On Cloud, S1 depends on a gateway release that forwards the service prefix and the document path with the same proof. It also depends on a chart change that configures them. The gateway lives in a separate repository, so that release is a dependency of S1, not a task of this repository.
This change states the forwarding contract that the release implements, as a `hosted-gateway-contract` requirement.

## Planned deltas for later stages

| Capability | Stage | What changes |
|---|---|---|
| `cell-service-identity` | S1, after #2852 merges | The discovery `service_api` section moves from `service-cell-contract` into this capability |
| `hosted-gateway-contract` | S2, S3 | Authenticate service principals and subject assertions into the proof; keep the paying account apart from the owner. This change adds the S1 forwarding requirement |
| `governance-kernel` | S2, S3 | Service audiences, custody grants and their default-deny; partitions and the narrowed owner exemption |
| `cloud-operator-access` | S3 | Operator exports and break-glass access keep member partitions out of reach |
| `hosted-vault-portability` | S3 | A readable export leaves member partitions out; a transfer keeps them protected |

## Ownership split

| Concern | Consumer product | Cell |
|---|---|---|
| Ownership | may provision, host and pay for the cell; acts as a service principal | belongs to the tenant, whose policy grants and bounds the product (D11) |
| End-user identity | authenticates its users; mints subject assertions at its edge | binds subjects from gateway-verified assertions |
| Media | acquires media and checks provider access | stores the passages it receives |
| Media processing | runs each component that a measured comparison on its data places in the consumer | runs a component only when that comparison places it in the cell |
| Desired state | decides what each record should be in the cell; schedules convergence | reports what it holds and its index state |
| Storage and ranking | none | canonical store, embeddings, passage ranking |
| Governance | none; its filters only narrow | principals, custody, kernel decisions, partitions |
| Erasure | decides what to erase and retains its intents | erases with honest progress and receipts |
| Audit | reads the export | records every call and governed disclosure |
| Product | surfaces, prompts, answers, citation checks, score calibration | none |

Today transcription and segmentation live in the consumer, and embedding lives in the cell. A component moves only after a measured comparison names its new home.

## Stages

| Stage | Outcome | Host setting, default off | Proof | Depends on |
|---|---|---|---|---|
| S0 | Enrolled Sources stay off shared generated pages | none | Proven in the passage-evidence delivery (D5) | #2852 |
| S1 | Typed service API, first version | `EXOMEM_SERVICE_API` | Each operation returns equal data through both doors on one disposable cell; every response validates against the document; the Cloud boundary refuses as on `/mcp`; passage bars hold at the service door | #2852 merged; the gateway release and chart change (D13) |
| S2 | Service principals under the tenant's grant (D6, D11) | `EXOMEM_SERVICE_PRINCIPALS` | Two principals on one cell cannot reach each other's Sources through any operation or argument; a principal without a committed grant reaches nothing; a narrowed or revoked grant refuses the next request; the tenant's own agents and the product reach only their own scopes; existing audiences' decisions are unchanged | S1; containment D1 in force on Cloud |
| S3 | Subject-bound requests and private member partitions (D7, D12) | `EXOMEM_SERVICE_SUBJECTS` | Two subjects cannot read each other's partitioned items through any read door, including hostile filters and references; the owner reads a partition only under a grant, and each such read writes a receipt; without a grant the owner receives aggregates only | S2; the partition fact of the single store |
| S4 | Audit export | `EXOMEM_SERVICE_AUDIT_EXPORT` | An exported window verifies end to end against the chain; an injected gap reports incomplete | S1; S2 for principal ids; a ledger retention rule |
| S5 | Subject erasure (D12) | the host erasure policy, per principal | After a member's erasure request completes, no door returns an item of the partition; progress reports complete, pending, failed or unknown | S3; `erase-captured-source` |

Each stage after S1 writes its requirements into a successor change before its code starts, and cites these decisions.

## Dependencies on in-flight work

- **Passage evidence and inventory (in review, #2852).** S1 renders their operations and lands after the merge. That delivery adds the Stage 0 requirement (D5). This change merges after #2852. This change edits none of their specs.
- **`erase-captured-source`.** `sources.erase` joins the first version when it ships. Discovery omits it until then. S5 reuses its owner acknowledgements.
- **`contain-cloud-controller-compromise`.** S1 follows its proof migration on the service prefix (D13). S2 relies on its proof-only requests on Cloud.
- **The Cloud gateway.** S1 on Cloud needs a gateway release from its separate repository and a chart change (D13).
- **The consumer's retrieval switch.** It runs on the MCP door and is not blocked. The consumer moves its transport after S1, behind its own setting, and moves its credential after S2.
- **`make-vault-store-canonical`.** S3's partition fact follows its page admission facts. No stage changes its contract.

## Load and latency

- **Per-cell rate target.** One cell serves 8 passage searches per second, sustained, with four concurrent callers. Four callers whose searches finish within the 500 ms four-caller bar complete at least that rate. Captures and settlements run one writer at a time. S1 measures the target before a host enables the service API.
- **Passage search.** The passage reference applies at the service door: 1,500 Sources, 40,000 declared-passage chunks, two CPUs, p95 below 200 ms with one caller and below 500 ms with four.
- **Inventory.** It keeps its reference of 20,000 Sources and pages of 200, with p95 below 200 ms.
- **Capture and settlement.** #2852 measured captures of 10 to 23 s against a 30 s client timeout, and its current round shortens them. The service door adopts the capture bar that #2852 sets, rather than a second bar, and its capture p95 must not exceed the MCP door's. Reservation and settlement are small writes with a p95 budget of 1 s. S1 measures all three at both doors in one run.
- **Request budget.** A service call carries the same request-scoped deadline as an MCP call (`connector-request-budget`).
- **Per request.** The service door is stateless HTTP and holds no session. It does the leaf's work plus schema validation, so it must not be slower than the MCP door.
- **Ledger growth.** Every service call adds one content-free row. Rotation bounds the live file, but the call ledger has no retention rule for rotated archives. That gap predates this change. S4's export window depends on a retention rule, so S4 adds it.
- **Fleet.** Cells per node is a Cloud platform measurement. It gates wide rollout of a consumer, not this change.

## Risks / Trade-offs

- **Two doors to one leaf can drift.** → One registration and one leaf per operation; the conformance check runs every operation through both doors.
- **Extracting one renderer can change the personal facade.** → The personal profile's existing tests and document stay unchanged as the acceptance check. A new test pins the status of an authority code and of a busy code on both profiles (task 1.5). No existing test pins the service side of either code.
- **Typed result models add code to each rendered leaf.** → Seven operations in the first version; the models also replace the consumer's hand-written parsers.
- **The OpenAPI diff tool can miss a semantic change.** → A meaning change still needs review; the committed document makes every schema change visible.
- **String codes give up enum checks in clients.** → The known values stay in the document; the conformance run fails on an undeclared code.
- **The service path depends on a gateway release in another repository.** → S1 stays off on Cloud until that release and the chart change ship.
- **Service credentials stay owner-equivalent until S2.** → D8 keeps per-person content out until S3; tenant isolation holds through one cell per tenant.
- **The tenant can narrow or revoke the product's grant, or leave with the cell.** → D11 makes these refusals, not outages; the product keeps its own records and stops using the cell.
- **A service can assert the wrong subject.** → D7 confines minting to the service's authentication edge; Exomem enforces everything after it.

## Migration and rollback

1. Ship S1 with `EXOMEM_SERVICE_API` unset on every cell. Behaviour is unchanged.
2. Ship the gateway release and the chart change that forward the service prefix and the document path.
3. Enable the first version on a disposable cell. Run the conformance check and the consumer's acceptance through its generated client.
4. Enable the first version on a tenant's cell, then switch the consumer's transport for that tenant through its own setting.
5. To roll back, switch the consumer back to the MCP door and unset the host setting. No data changes, because both doors call the same leaves.

## Open Questions

These choices belong to the operator. Each has a recommendation.

1. **Does Exomem publish generated client packages?** Recommend publishing only the document until a second consumer exists.
2. **Do hosted and self-hosted enterprise cells get the service API in S1?** Recommend Cloud cells first. Other cell modes get the same setting when they have a verifier that their own operator cannot swap, and matching discovery.
3. **How long does a superseded version stay?** Recommend at least 90 days after its successor ships, advertised in discovery.
4. **Who verifies subjects?** Recommend service-signed assertions that the gateway verifies, because the consumer is its users' identity provider. Accept external identity-provider tokens later for enterprises that have one.
5. **When does the consumer move off the MCP door?** Recommend right after S1, before its rollout passes the first tenant.
6. **Where is the load target recorded?** Recommend the per-cell target above, plus a cells-per-node target from the Cloud platform, recorded once in the repository.
7. **Who may issue the owner's read grant for one member's partition (D12)?** Recommend the owner, for a stated purpose and a bounded expiry, with each read audited.
8. **What happens to member partitions when the product's grant ends (D11, D12)?** Recommend that they stay closed to the owner and are erased after a retention window that the tenant's policy sets.
