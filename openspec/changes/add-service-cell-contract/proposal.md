## Why

Outside services now use Exomem cells as their memory and knowledge layer. The first consumer is a multi-tenant question-answering product with one cell for each tenant.
It captures records as declared passages, ranks them with passage search and reconciles them through inventory.
It reaches the cell through the MCP door, which is shaped for agents. Its client therefore parses agent replies, digs leaf results out of diagnostics and calls service commands that the door hides but still accepts.

That contract is not fit for a service or for an enterprise integrator:

- It has no version. Agent-surface work can change a reply that a service depends on.
- It has no typed result or refusal code. Each consumer hand-builds a parser.
- A service credential is the owner. On a Cloud cell the cell bearer resolves to the owner audience, so a service reads everything in its cell.
- The next uses of the same cell put per-person content in it: tenant memory, member-private memory and member feedback. The cell must then enforce who may read each item, from authenticated facts, whatever filters the caller sends.

This change decides the integration contract and ships its first step.
The design decision that warrants a change is the contract itself. A service reaches a cell through a versioned, typed profile of the cell's one HTTP renderer of the command registry. It does not use the agent door or an embedded library. Its authority comes from its own principal, custody of its identity systems and the governance kernel.

## What Changes

- **Stage 1: a typed service API, off by default.** A Cloud cell whose host enables a version serves each service operation at `POST /service/<version>/<operation>`. A version is named by the month in which it is cut.
  - Each operation renders one registered command and calls its leaf inside the full MCP boundary: bearer, gateway proof, containment verdict, connector boundary, request budget and call ledger.
  - The service profile and the personal REST facade share one HTTP renderer. The personal facade does not change.
  - Each version has one fixed OpenAPI 3.1 document for each release, served beside discovery. Discovery advertises the document digest and the operations that the cell serves.
  - Requests are closed, and results are open with every field required. Codes and result states are strings, so additions do not break generated clients.
  - Refusals map to fixed status classes through the one code-to-status map that every HTTP door of the cell shares.
  - A version changes only by addition, and CI checks this with `oasdiff`.
- **The client is generated, not hand-built.** Exomem publishes the contract document. A consumer generates its client with a maintained OpenAPI generator.
- **Two ownership rules are decided.** The tenant owns a cell that a product provisions for them, even when the product hosts and pays for it. The product reaches the cell as a service principal under the tenant's policy. A member's partition is private from the owner by default: the owner reads it only under an explicit, audited grant and otherwise sees aggregates. Stages 2 and 3 implement these rules.
- **Later stages are recorded as design.** They are service principals (S2), subject-bound requests (S3), an audit export (S4) and subject erasure (S5). Each stage writes its requirements in a successor change before its code starts.
- Agents keep the MCP tools unchanged. Stage 1 grants no authority that the MCP door does not grant.

The service API, and each later stage, is default-off behind host configuration. When the host does not enable a stage, the cell behaves exactly as today. No stage runs a model.

## Capabilities

### New Capabilities

- `service-cell-contract`: the versioned, typed service profile of the command registry, its document, compatibility and refusal rules, its boundary and its latency budgets.

### Modified Capabilities

- `call-ledger`: a service API call writes exactly one content-free row, which names the service door, the version and the operation.
- `connector-request-budget`: a service API call carries the same request-scoped deadline as an MCP tool call.

Later stages write their own deltas when they start: `cell-service-identity` after #2852 merges, and `hosted-gateway-contract`, `governance-kernel`, `cloud-operator-access` and `hosted-vault-portability`. The design lists each one.

## Impact

- **Code.** The change touches the command registry and the result models of the rendered leaves. It extracts one HTTP renderer from the personal REST facade and adds the service profile to it. It extends the code-to-status map, the Cloud origin middleware's path match, discovery, the request budget and the call ledger.
- **Dependencies.**
  - Stage 1 renders the operations of the passage-evidence and captured-source-inventory work (#2852), which is in review, and lands after it merges.
  - Stage 0 keeps service captures off the shared summary pages. It waits on #2852, which adds that requirement and amends `erase-captured-source` to match.
  - The erase operation joins the first version when `erase-captured-source` ships.
  - Stage 1 follows the proof migration of `contain-cloud-controller-compromise` on the service path.
  - Stage 3 stores subject partitions; its storage follows `make-vault-store-canonical`.
  - None of this blocks or reopens that work.
- **Consumers.** A product must tolerate a tenant who narrows or revokes its grant, or who leaves with their cell. A consumer keeps its MCP client until Stage 1 ships. It then switches its transport to the generated client behind its own setting. Its capture, convergence and verification logic stays the same, because the leaves are the same.
- **Operations.** Stage 1 adds one host setting, `EXOMEM_SERVICE_API`. It adds no listener, store or credential. On Cloud the gateway forwards only the MCP path today. Stage 1 therefore needs a gateway release and a chart change that forward the service path and the document path with the same proof. The gateway lives in a separate repository.
