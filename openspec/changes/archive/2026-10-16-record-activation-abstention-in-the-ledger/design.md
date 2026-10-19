# Design

## Context

See `proposal.md` for the accounting defect.
The synchronous wrapper runs in a worker thread whose copied context does not propagate new context-variable assignments to middleware.
The existing request budget already shares one mutable object through that copied context.
The ledger and offline readers use schema version 1 and hash the complete stored row.

## Goals / Non-Goals

Record the final activation packet's strict abstention flag without reading its material or inferring quality.
Keep observations request-local and preserve existing refusal, exception and serialization precedence.
This repair changes accounting only. It adds no benchmark call, model, retrieval policy, deadline or production control.

## Decisions

### Reuse the request context

Allocate one fresh mutable activation observation cell in `mcp_request_context` for each activation call.
The worker writes bounded primitive facts after the final wrapper postfilter; middleware reads the cell before context reset.
Keep the independently observed strict abstention flag even if reason or budget metadata is malformed.
Nested contexts restore their parent. Concurrent calls receive distinct cells, including calls with equal request identifiers.
A late cancelled worker can update only its abandoned cell.
This reuses the budget transport and avoids a global token map, expiry policy, locks or middleware result decoding.

### Define one closed result protocol

A small `activation_result` leaf owns the six fixed reason values and the bounded projection.
Existing activation producers import its plain string values; unrelated statuses retain their current ownership.
Unknown reason values become null. Invalid boolean values remain unknown, without truthiness coercion.
Serialize the optional block only when its two boolean observations are valid.
The separate strict abstention flag still determines the outcome when that block cannot be built.
Telemetry failures must never change the tool response.

### Preserve outcome precedence

Exceptions produce `error`, and refusal breadcrumbs produce `refused`.
A completed activation with strict abstention produces `abstained`, with a null error code.
Other completed calls produce `ok`.
Neither `ok` nor a false budget flag certifies useful, complete or relevant material.

### Keep historical semantics unknown

Retain schema version 1, with an additive optional block and extended outcome vocabulary.
Ship strict readers with the producer. Preserve all historical bytes and hashes.
A legacy activation row with `ok`, no error and no observation can join a successful client timing observation, including client abstention.
Keep that row's raw server outcome and unknown server activation semantics separate from the client facts.
Current recorded disagreements and refusal or error mismatches continue to reject the timing join.
Ledger summaries report observation coverage separately from execution outcomes and timing.

## Risks / Trade-offs

- Thread context copies can hide assignments: share a fresh mutable cell and prove the real worker-to-middleware path.
- Partial metadata can erase abstention: retain the strict flag independently and prove malformed metadata cases.
- Older strict readers reject the new outcome: deliver all repository readers in the same batch.
- A private reason can enter telemetry: project through the fixed protocol and retain no packet reference or content.
- Historical rows cannot establish semantic success: display unknown coverage without rewriting them.

## Migration Plan

1. Deliver the producer and strict readers together; mixed historical chains remain readable and verifiable.
2. Restart through the ordinary release procedure; each new request receives a fresh observation cell.
3. If rollback is needed, retain readers that accept `abstained`; stored rows and hashes remain intact.
