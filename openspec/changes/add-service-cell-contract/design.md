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
- **The personal REST facade exists, but not on cells.** `/api/<tool>` renders the command registry over HTTP with an OpenAPI document. It serves local installs with the owner's REST key. Most of its result schemas are untyped.
- **The governance kernel already decides disclosure per audience.** Scopes, standing rules, grants, purpose and organisation caps map an item to a ceiling from L0 to L6. A scope can deny every audience that no rule names, and a residual scope selects what no other scope selects. The owner is exempt from that default-deny.
- **Connector ceilings exist.** A host-owned configuration gives authenticated OAuth clients of one owner different denied and writable scope sets. A shared cell credential receives the restricted default.
- **The call ledger records MCP calls only.** Disclosure receipts record governed egress in a hash chain.
- **The single store is in flight.** `make-vault-store-canonical` makes one SQLite store canonical, with Markdown as a projection. Its task 6.2 lets a Cloud cell run without per-write projection.

A service uses a cell in one of two disclosure modes. The design treats them differently:

- **Index-only evidence.** The cell returns keys, hashes and scores. The service reloads and authorizes its own records. A cell defect can cost ranking quality, but it cannot disclose text. Today's retrieval uses this mode.
- **Cell-authoritative content.** The cell returns text that the service shows or puts in a prompt: tenant memory, member-private memory, member feedback. Here the cell alone decides who may read each item.

## Goals / Non-Goals

**Goals:**

- A service integrates through one deterministic, versioned, typed contract. It needs no knowledge of agent replies.
- Exomem keeps one implementation of each operation. Agents and services reach the same leaves.
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

Keep MCP as the only door. Give each service command an output schema, return structured content without agent text, and document the service tool names.

- **Integrator, day one:**
  1. Receive the cell address and a credential from the operator.
  2. Connect an MCP client library and run the initialize handshake.
  3. Call `tools/call` with service tool names from the documentation, because tool listing hides them.
- **First consumer changes:** it reads structured content against the output schema instead of diagnostics. It keeps its MCP session pool and portal machinery.
- **Verdict: rejected.** MCP has no contract version, so agent-surface work and service stability pull against each other. Agent replies must lead with a sentence and put codes in diagnostics; a service needs the reverse. The session handshake adds a stateful connection to every service. A security review must assess an agent protocol to approve a back-end integration.

### B. A typed service rendering of the registry, with a generated client

Serve each service operation at a versioned HTTP path, rendered from the same registered command and leaf. Publish an OpenAPI 3.1 document. Consumers generate their client from it.

- **Integrator, day one:**
  1. Receive the cell address and a credential from the operator.
  2. Read discovery and compare the cell identity, the served versions and the document digest.
  3. Download the document and generate a client with a maintained OpenAPI generator.
  4. Call `POST /service/v1/sources.reserve_capture` and the other operations. Handle refusals by code and HTTP status.
- **First consumer changes:** it replaces its MCP transport with the generated client and a pooled HTTP client. It maps closed refusal codes to its own outcomes. Its convergence, verification, score map and readiness logic stay, because the leaves and their semantics do not change.
- **Verdict: recommended.** It uses mature tooling everywhere, keeps one implementation, and lets the service contract evolve on its own version line.

### C. An embedded library

Ship Exomem as a library that the service opens in its own process over a per-tenant store file. The package API is the SDK.

- **Integrator, day one:**
  1. Install a pinned Exomem release.
  2. Provision store files and embedding models on its own hosts.
  3. Call library functions in its own process.
- **First consumer changes:** it removes the network hop, discovery and credentials. It takes over storage, backup, model serving and erasure of the files on its hosts.
- **Verdict: rejected.** Governance would run in the caller's process, so a caller defect could open another tenant's file. Erasure proofs and operator-access limits would depend on the caller's hosts. It serves one language only.

## Decisions

### D1. Services use a versioned rendering of the command registry

A **service operation** names one registered command. It fixes or narrows that command's arguments and declares a request model, a result model and its refusal codes. It resolves to its command before authorization, as MCP renderings do. It never adds behaviour that the leaf lacks.

The cell serves `POST /service/<version>/<operation>` on its existing listener when the host setting `EXOMEM_SERVICE_API` lists that version. Every request is POST with a JSON body, so no query text, content or identifier appears in a URL or a proxy log.

The version 1 set renders the commands that the first service already calls:

| Operation | Command |
|---|---|
| `sources.reserve_capture` | Source capture reservation |
| `sources.capture` | `capture_source`, with declared passages and an attempt required |
| `sources.settle_capture` | Source capture settlement |
| `sources.inventory` | captured-Source inventory |
| `sources.erasure_policy` | Source erasure policy |
| `sources.erase` | captured-Source erasure, when `erase-captured-source` ships |
| `passages.search` | `search_memory` at the passage result level |

Discovery stays the content-free entry point. It gains a `service_api` section that lists each served version with its document path and SHA-256 digest.

The cell's HTTP stack is FastMCP on Starlette, and the personal REST facade already generates OpenAPI from the registry. The service route group reuses that generation. FastAPI was the closest alternative. It would add a second routing and validation layer beside the registry binding.

### D2. The contract document is the SDK

Exomem publishes one OpenAPI 3.1 document for each version. A generated copy is committed in the repository, so a contract change is visible in review. A test keeps that copy equal to the generation.

Consumers generate clients with maintained tools, such as `openapi-python-client` or `openapi-typescript`. Exomem writes and maintains no client by hand. Whether Exomem also publishes generated client packages is an open choice.

Each operation states in the document whether a repeat of the same request is safe, and which identifier makes it safe. The capture protocol already supplies these identifiers: the attempt handle and the erasure operation identifier. The service API adds no generic replay layer, because the capture protocol bypasses generic replay by design.

### D3. A version changes only by addition

Within one version a release may add operations, optional request fields, response fields and refusal codes inside the documented status classes.
These changes need a new version: a removal, a rename, a change of type or meaning, a new required field, or a refusal of an accepted value.
The cell serves both versions until the earlier version's retirement date, which discovery advertises.
CI compares the generated document with the committed one through a maintained OpenAPI diff tool, such as `oasdiff`. It refuses a breaking difference within a version.

### D4. Refusals carry closed codes in fixed status classes

Every refusal carries one code that the document lists for that operation. Its HTTP status comes from one fixed table:

| Status | Class | Client action |
|---|---|---|
| 400 | invalid request | change the request; the error names the field |
| 401 | credential refused | stop; check the registration |
| 403 | authority refused | stop; the principal lacks authority |
| 404 | unknown route or version | stop; read discovery |
| 409 | conflict or stale precondition | read current state, then decide |
| 500 | unexpected failure | treat the outcome as unknown; read state, or repeat with the same identifier when the operation states it is safe |
| 503 | busy or unavailable | retry after the hint |

A client generated from an earlier document can act on a new code through its status class.
A busy cell refuses at once with a retry hint and the `Retry-After` header. It does not queue.
An uncertain outcome, such as a pending settlement, is a typed result state, not an error.
A message can accompany a code for people. A client acts on the code alone.
The codes and the status table are protocol tokens that this specification fixes, so they are a closed set and not vocabulary.

### D5. Service captures stay off shared summaries

Attempt reservation is a service-tier command that the agent tool listing does not offer, so an attempt-bound capture comes from a service.
It does not write the Sources catalog, the top-level index or the activity log. Captured-Source inventory is its listing.
A service capture appears in no shared summary that a person reads, and its erasure has no summary fragment to clean.
This stage has no flag of its own. The attempt protocol has not shipped in a release, so no released caller depends on these summary writes.

### D6. Service authority has three layers (Stage 2)

A **service principal** is registered by the host operator, not by the service. A host-owned registry file outside the vault holds each entry: an opaque principal id, its credential verifiers with rotation, and the identity systems it holds in custody.

1. **Principal.** The credential resolves to a non-owner audience in the kernel's identity space, under a reserved prefix that no OAuth subject can produce. The owner credential stays with the operator.
2. **Custody.** A principal has full authority over the Sources of the identity systems it holds: capture, settlement, inventory, passage search and erasure. This replaces whole-namespace authority for that principal. Two principals cannot hold one system. The host erasure policy still enables erasure per system.
3. **Kernel.** Every other item reaches the principal only through the kernel's decision for its audience. A service audience that no standing rule names receives no disclosure, also for an item outside every scope. This default is an evaluator rule, not a policy document, so a policy edit cannot remove it.

When consumers have moved to their own principals, the agent door refuses service operations. The connector boundary stays as it is for OAuth clients.

### D7. Exomem binds subjects from verified assertions (Stage 3)

A service request MAY carry a **subject assertion**: a short-lived JWS signed with a key that the principal's registry entry names, with the cell as audience.
Exomem verifies it and derives the subject's audience id with the kernel's existing normalization of issuer and subject, using the principal as issuer. Two services' subjects therefore never share an id.

- An item written in a subject-bound request belongs to that subject's partition. Exomem sets the partition. No request field can set or change it.
- The kernel discloses a partitioned item only to a request bound to the same subject, within the principal's own ceiling.
- A request without an assertion sees no partitioned item.
- Filters, tags and references in a request only narrow this result.

Exomem cannot detect a service that asserts the wrong subject. The service mints assertions only at its authentication edge and carries them in request context, so ordinary business code never chooses a subject.
The partition key needs a page fact. In store mode it belongs with the page admission facts of `make-vault-store-canonical` (D13). The Stage 3 change decides whether file mode gets an equivalent or Stage 3 requires store mode.

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

## Ownership split

| Concern | Consumer product | Cell |
|---|---|---|
| End-user identity | authenticates its users; mints subject assertions at its edge | verifies assertions; binds subjects |
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
| S0 | Service captures stay off shared summaries | none (D5) | An attempt-bound capture leaves the three summary pages byte-identical and appears in inventory; an ordinary capture still updates them | passage-evidence merged |
| S1 | Typed service API v1 | `EXOMEM_SERVICE_API` | Each operation returns the same data through both doors on one disposable cell; every response validates against the document; passage bars hold at the service boundary | passage-evidence merged |
| S2 | Service principals | `EXOMEM_SERVICE_PRINCIPALS` names the registry file | Two principals on one cell cannot reach each other's Sources through any operation or argument; an ungoverned cell gives a principal custody only; existing audiences' decisions are unchanged | S1 |
| S3 | Subject-bound requests | a subject key in a principal's registry entry | Two subjects cannot read each other's partitioned items through any read door, including hostile filters and references | S2; the partition fact of the single store |
| S4 | Audit export | `EXOMEM_SERVICE_AUDIT_EXPORT` | An exported window verifies end to end against the chain; an injected gap reports incomplete | S1; S2 for principal ids |
| S5 | Subject erasure | the host erasure policy, per principal | After complete erasure no door returns an item of the partition; progress reports complete, pending, failed or unknown | S3; `erase-captured-source` |

Each stage after S1 writes its requirements into a successor change before its code starts, and cites these decisions.

## Dependencies on in-flight work

- **Passage evidence and inventory (in review).** S0 and S1 render their operations and land after the merge. This change edits none of their specs.
- **`erase-captured-source`.** `sources.erase` joins version 1 when it ships. Discovery omits it until then. S5 reuses its owner acknowledgements.
- **The consumer's retrieval switch.** It runs on the MCP door and is not blocked. The consumer moves its transport after S1, behind its own setting, and moves its credential after S2.
- **`make-vault-store-canonical`.** S3's partition fact follows its page admission facts. No stage changes its contract.

## Load and latency

- **Per cell.** The passage reference applies at the service boundary: 1,500 Sources, 40,000 declared-passage chunks, two CPUs, p95 below 200 ms with one caller and below 500 ms with four. Inventory keeps its reference of 20,000 Sources and pages of 200.
- **Per request.** The service door is stateless HTTP and holds no session. It does the leaf's work plus schema validation, so it must not be slower than the MCP door. S1 measures both doors in one run.
- **Fleet.** Cells per node is a Cloud platform measurement. It gates wide rollout of a consumer, not this change.

## Risks / Trade-offs

- **Two doors to one leaf can drift.** → One registration and one leaf per operation; the conformance check runs every operation through both doors.
- **Typed result models add code to each rendered leaf.** → Seven operations in version 1; the models also replace the consumer's hand-written parsers.
- **The OpenAPI diff tool can miss a semantic change.** → A meaning change still needs review; the committed document makes every schema change visible.
- **Service credentials stay owner-equivalent until S2.** → D8 keeps per-person content out until S3; tenant isolation holds through one cell per tenant.
- **A service can assert the wrong subject.** → D7 confines minting to the service's authentication edge; Exomem enforces everything after it.

## Migration and rollback

1. Ship S0 and S1 with `EXOMEM_SERVICE_API` unset on every cell. Behaviour is unchanged.
2. Enable version 1 on a disposable cell and run the conformance check and the consumer's acceptance through its generated client.
3. Enable version 1 on a tenant's cell, then switch the consumer's transport for that tenant through its own setting.
4. To roll back, switch the consumer back to the MCP door and unset the host setting. No data changes, because both doors call the same leaves.

## Open Questions

These choices belong to the operator. Each has a recommendation.

1. **Who owns a product-provisioned cell?** Recommend that the tenant is the owner and the product is a service principal under the tenant's policy.
2. **May the owner read member partitions?** Recommend no by default: the owner can erase and export, and can read only through an explicit, audited grant. This narrows the kernel's owner exemption for partitioned items.
3. **Does Exomem publish generated client packages?** Recommend publishing only the document until a second consumer exists.
4. **Do hosted and self-hosted enterprise cells get the service API in S1?** Recommend Cloud cells first. Other cell modes get the same setting when their credential and discovery match.
5. **How long does a superseded version stay?** Recommend at least 90 days after its successor ships, advertised in discovery.
6. **Who verifies subjects?** Recommend service-signed assertions, because the consumer is its users' identity provider; accept external identity-provider tokens later for enterprises that have one.
7. **When does the consumer move off the MCP door?** Recommend right after S1, before its rollout passes the first tenant.
8. **What load target does a service cell record?** Recommend the passage reference per cell, plus a cells-per-node target from the Cloud platform, recorded once in the repository.
