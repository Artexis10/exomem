## Why

Outside services now use Exomem cells as their memory and knowledge layer. The first consumer is a multi-tenant question-answering product with one cell for each tenant.
It captures records as declared passages, ranks them with passage search and reconciles them through inventory.
It reaches the cell through the MCP door, which is shaped for agents. Its client therefore parses agent replies, digs leaf results out of diagnostics and calls service commands that the door hides but still accepts.

That contract is not fit for a service or for an enterprise integrator:

- It has no version. Agent-surface work can change a reply that a service depends on.
- It has no typed result or closed refusal code. Each consumer hand-builds a parser.
- A service credential is the owner. On a Cloud cell the cell bearer resolves to the owner audience, so a service reads everything in its cell.
- The next uses of the same cell put per-person content in it: tenant memory, member-private memory and member feedback. The cell must then enforce who may read each item, from authenticated facts, whatever filters the caller sends.

This change decides the integration contract and ships its first step.
The design decision that warrants a change is the contract itself. A service reaches a cell through a versioned, typed rendering of the shared command registry, not through the agent door or an embedded library. Its authority comes from its own principal, custody of its identity systems and the governance kernel.

## What Changes

- **Stage 1: a typed service API, version 1, off by default.** A Cloud cell whose host enables it serves each service operation at `POST /service/v1/<operation>`. Each operation renders one registered command and calls its leaf through the same boundary as the MCP door. It has a strict request schema, a typed result, closed refusal codes and a published OpenAPI 3.1 document. Discovery advertises the version and the document digest. A version changes only by addition. Every call writes a call-ledger row.
- **The client is generated, not hand-built.** Exomem publishes the contract document. A consumer generates its client with a maintained OpenAPI generator.
- **Two ownership rules are decided.** The tenant owns a cell that a product provisions for them, even when the product hosts and pays for it. The product reaches the cell as a service principal under the tenant's policy. A member's partition is private from the owner by default: the owner reads it only under an explicit, audited grant and otherwise sees aggregates. Stages 2 and 3 implement these rules.
- **Later stages are recorded as design.** They are service principals (S2), subject-bound requests (S3), an audit export (S4) and subject erasure (S5). Each stage writes its requirements in a successor change before its code starts.
- Agents keep the MCP tools unchanged. Stage 1 grants no authority that the MCP door does not grant.

The service API, and each later stage, is default-off behind host configuration. When the host does not enable a stage, the cell behaves exactly as today. No stage runs a model.

## Capabilities

### New Capabilities

- `service-cell-contract`: the versioned, typed service rendering of the command registry, its compatibility and refusal rules, and its authority and audit invariants.

### Modified Capabilities

None. The stages that change existing capabilities (`governance-kernel`, `call-ledger`, `hosted-tenant-cell`) write their deltas when they start.

## Impact

- **Code.** The change touches the command registry and the result models of the rendered leaves. It adds a Starlette route group on the Cloud cell. It extends discovery and the call ledger. It reuses the OpenAPI generation of the personal REST facade.
- **Dependencies.** Stage 1 renders the operations of the passage-evidence and captured-source-inventory work, which is in review, and lands after it merges. That delivery (#2852) also owns Stage 0: service captures stay off the shared summary pages. This change relies on that rule and does not restate it. The erase operation joins version 1 when `erase-captured-source` ships. Stage 3 stores subject partitions; its storage follows `make-vault-store-canonical`. None of this blocks or reopens that work.
- **Consumers.** A product must tolerate a tenant who narrows or revokes its grant, or who leaves with their cell. A consumer keeps its MCP client until Stage 1 ships. It then switches its transport to the generated client behind its own setting. Its capture, convergence and verification logic stays the same, because the leaves are the same.
- **Operations.** Stage 1 adds one host setting, `EXOMEM_SERVICE_API`. It adds no listener, store or credential. The Cloud edge routes the service path as it routes the MCP path.
