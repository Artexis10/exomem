## Context

The released mutation path enters one vault-wide boundary before idempotency can identify an identical in-flight retry. FastMCP runs synchronous tools in worker threads, so cancellation of the transport response does not necessarily cancel the underlying write. A retry can therefore collide with the still-running worker and surface `MUTATION_BUSY` even when the original operation later commits. Validate-only edits are also classified as writes, and optional background reconciliation can hold the same boundary across a large batch without exposing owner or age.

The original design also covered a first registry-driven entity-capture increment. That increment has since been superseded by the shipped vault-extensible `entity-type-registry` and the kind-neutral `complete-recurring-entity-lifecycle` change. Its fixed-registry design and deltas are intentionally removed here rather than retained as conflicting active guidance.

## Goals / Non-Goals

**Goals:**

- Preserve `edit_memory` and its public name while making surgical validation and retry behavior reliable.
- Ensure identical retries observe one durable terminal outcome without entering the exclusive boundary twice.
- Make long mutation holders diagnosable and keep optional reconciliation work bounded.

**Non-Goals:**

- Fixing ChatGPT's host-side `Resource not found` router; Exomem can only keep its published surface stable and provide alternate session/client recovery guidance.

## Decisions

### 1. Claim the retry receipt before the vault boundary

Adopt the receipt-first design from draft PR #252. An identical principal/command/canonical-payload retry inspects or claims its durable receipt before competing for the exclusive mutation boundary. Pending identical work waits for a bounded terminal outcome outside the boundary; completed or committed-failure results replay. Different identities remain serialized and may receive a retryable busy response.

Alternative: automatically retry every busy mutation. Rejected because it cannot distinguish an acknowledgement-loss replay from a materially revised write and could duplicate committed work.

### 2. Treat `edit_memory(validate_only=true)` as read-only

The command classifier will mark only this exact invocation read-only. It will run structural and semantic preflight against guarded bytes but will never commit, acquire writer authority, create an idempotency receipt, or enter the vault mutation boundary. The normal compare-and-swap guard still protects the later real edit.

Alternative: let validation wait behind writes. Rejected because validation changes no state, provides no serialization benefit, and was itself used as a recovery probe in the production incident.

### 3. Expose bounded, content-free mutation-holder telemetry

The mutation coordinator will track opaque request ID, operation class, acquisition time, and holder kind (command/background/transfer). Status/readiness returns only owner kind, operation name, age, and threshold state—never arguments, paths, titles, or content. A warning is emitted when the configured long-holder threshold is crossed. Background reconciliation will release and reacquire between bounded items/batches rather than holding the global boundary across an entire backlog.

Alternative: forcibly break an in-process lock after a timeout. Rejected because the worker may still be committing; breaking ownership would violate atomicity.

## Risks / Trade-offs

- [Waiting identical retries can consume workers] → Bound pending waits and return `MUTATION_ACKNOWLEDGEMENT_PENDING` with correlation metadata after the deadline.
- [Long-holder telemetry can tempt unsafe lock breaking] → Observability only; never revoke a live holder.

## Migration Plan

1. Land the receipt-first replay tests and implementation from PR #252, extended with real `edit_memory` preflight and transport-cancellation cases.
2. Add validate-only classification, holder telemetry, and bounded background reconciliation.
3. Build and run focused/full suites, OpenSpec validation, package/tool-fingerprint checks, and an independent review.
4. Quiesce public mutations, deploy/restart only as required, then prove health, discovery, validation without lock acquisition, and cancelled-edit retry. Roll back to the prior wheel if readiness or write smokes fail.

## Open Questions

None. Entity ontology and lifecycle work are owned by the current canonical registry and recurring-entity changes rather than this historical mutation-safety delta.
