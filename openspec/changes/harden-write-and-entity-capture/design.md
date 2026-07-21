## Context

The released mutation path enters one vault-wide boundary before idempotency can identify an identical in-flight retry. FastMCP runs synchronous tools in worker threads, so cancellation of the transport response does not necessarily cancel the underlying write. A retry can therefore collide with the still-running worker and surface `MUTATION_BUSY` even when the original operation later commits. Validate-only edits are also classified as writes, and optional background reconciliation can hold the same boundary across a large batch without exposing owner or age.

Entity capture has a separate single-source failure. `link.py`, index refresh, scaffold prose, the capture hook, command descriptions, and pack metadata each carry pieces of the entity contract. The capture hook enumerates only note-shaped stepping stones, `link.py` hard-codes four entity kinds, and Organizations do not exist. Knowledge packs already carry `default_entity_types`, while the epistemic graph already indexes typed pages and relations, but neither drives entity authoring.

## Goals / Non-Goals

**Goals:**

- Preserve `edit_memory` and its public name while making surgical validation and retry behavior reliable.
- Ensure identical retries observe one durable terminal outcome without entering the exclusive boundary twice.
- Make long mutation holders diagnosable and keep optional reconciliation work bounded.
- Define entity kinds once and use that registry for validation, folders, indexes, bootstrap guidance, pack validation, and capture guidance.
- Add Organizations and let selected packs dynamically prioritize supported entity kinds without changing their storage contract.
- Make proactive entity capture conservative, identity-aware, and update-first.

**Non-Goals:**

- Server-side LLM entity extraction, autonomous entity creation, or confidence scoring.
- Creating an entity for every proper noun or backfilling every historical note automatically.
- Silently rewriting existing rich entity pages into a new template.
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

### 4. Use one internal entity registry

Add an `entity_types` module with immutable definitions. A definition contains a stable ID, plural folder label, display label, aliases, and capture guidance. The registry preserves `person`, `concept`, `library`, and `decision`, and adds `organization` under `Entities/Organizations/`. Pack `default_entity_types` values are validated against this registry and act as capture priorities, not a second validity list.

Alternative: keep expanding Python tuples and prose. Rejected because that is the drift mechanism that caused this bug. Alternative: allow arbitrary pack/vault-defined folders in this urgent release. Rejected because it changes the durable authoring contract, index migration, and compatibility surface before those semantics are designed. The registry gives a clean future extension seam without pretending arbitrary types are safe today.

### 5. Keep capture reasoning agent-side, driven by measured registry state

The hook remains a small advisory and stops enumerating entity types. It asks whether the session produced either a durable conclusion or a durable recurring entity recognized by the registry, with selected packs prioritizing relevant kinds. Bootstrap returns the registry and a deterministic routing rule: search exact/alias candidates first; if an active entity exists, use `edit_memory` for a small correction and `connect_memory` for relations; otherwise create through `connect_memory(operation="create-entity")` only when the identity is durable, recurring, central to the conclusion, and useful beyond the current source.

The server does not infer entities from prose. This preserves the pure-substrate boundary: Exomem supplies registry and graph measurements; the reasoning agent decides whether an entity is warranted.

### 6. Generate dependent surfaces and fail on drift

`link` validation/rendering, entity folders/index counts, bootstrap catalogs, command descriptions, scaffold reference tables, and pack validation will consume the registry or generated registry documentation. Tests assert that no independent entity-kind enumeration remains in executable guidance and that every registered kind is creatable, indexable, discoverable, and represented in bootstrap.

## Risks / Trade-offs

- [Future entity kinds can require a release] → One registry and generated/drift-tested dependents make additions deliberate and reviewable; arbitrary runtime extensions stay out of this hotfix.
- [Proactive guidance can create entity spam] → Require durable identity plus recurrence/usefulness; exact/alias search first; no server-side auto-create.
- [Two entity pages can refer to one identity] → Normalize aliases for lookup, return ambiguity rather than silently merging, and require governed reconciliation.
- [Registry changes can alter tool descriptions/fingerprints] → Treat the generated surface change as intentional, version/release it, refresh the connector contract once, and keep invocation names stable.
- [Waiting identical retries can consume workers] → Bound pending waits and return `MUTATION_ACKNOWLEDGEMENT_PENDING` with correlation metadata after the deadline.
- [Long-holder telemetry can tempt unsafe lock breaking] → Observability only; never revoke a live holder.

## Migration Plan

1. Land the receipt-first replay tests and implementation from PR #252, extended with real `edit_memory` preflight and transport-cancellation cases.
2. Add validate-only classification, holder telemetry, and bounded background reconciliation.
3. Add the entity registry with compatibility definitions for all existing kinds, then Organizations, pack-priority validation, and registry-driven indexes/guidance.
4. Build and run focused/full suites, OpenSpec validation, package/tool-fingerprint checks, and an independent review.
5. Release a new pre-1.0 minor version because the public entity/tool guidance expands.
6. Quiesce public mutations, deploy/restart the Windows service and tunnel only as required, then prove health, discovery, validation without lock acquisition, cancelled edit retry, organization create/read/edit, and ordinary existing entity compatibility.
7. Refresh/promote the connector contract and perform a fresh ChatGPT session smoke. Roll back to the prior wheel if readiness or write smokes fail; existing Markdown remains compatible because no migration rewrites existing pages.

## Open Questions

None. The user approved the registry-driven entity model, Organizations, conservative proactive capture, retention of `edit_memory`, and inclusion in the launch-hardening release.
