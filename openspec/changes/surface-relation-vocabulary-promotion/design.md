## Context

Compiled-note feedback already reports structural blocks, sources, links, relations, suggestions, and next actions. It counts relation rows but does not distinguish labels that the active relation registry cannot resolve. The semantic preflight currently treats that same observation as an error, so the public write never reaches feedback construction. Separately, `infer_relation_registry` already groups observations by raw label, counts them, and returns a copy of the active proposal, but it only inserts single-observation namespaced labels into that proposal.

The governed save path is intentionally separate: callers review and complete a proposal, then explicitly request `save=true`; overwrite uses `expected_hash`, and observed labels cannot be silently deleted.

## Goals / Non-Goals

**Goals:**

- Surface exact unregistered relation labels inside existing write feedback, with the existing `schema_memory` promotion route and a next action.
- Let a write with an independently qualifying connection commit while reporting the unregistered observation as advisory.
- Insert recurring unregistered labels into the inferred extension proposal at a default threshold of three observations.
- Leave the proposed parent and description unset so a human chooses the semantics.
- Preserve read-only inference and all existing save guards.

**Non-Goals:**

- Blocking writes that contain unregistered relation rows.
- Letting an unregistered row satisfy typed-edge or connectivity qualification.
- Inferring a core parent, description, alias, namespace, or epistemic meaning.
- Automatically saving a proposal or changing command/tool signatures.

## Decisions

### Separate advisory vocabulary feedback from relation qualification

An authored fact whose relation is unregistered produces a warning-level `unregistered_relation` finding instead of an independent blocking error. The fact remains unresolved by the registry, so the existing typed-edge and connectivity predicates continue to reject it. A write therefore succeeds only when another governed connection, reviewed-none disposition, or bootstrap rule independently satisfies the relation gate. An unknown-only relation row still blocks on the missing relation disposition.

Deprecated relations and scope violations remain errors. This change is limited to the unregistered observation that the feedback and proposal workflow can now surface for review.

### Resolve feedback labels against the active registry

After semantic preflight accepts a separately connected write, the feedback builder parses the normalized write body once, collects note-level and block-level relation labels, and calls `registry.resolve(kind)` for each distinct label. Only `status == "unregistered"` creates a signal; core, extension, alias, deprecated, and scoped resolutions are left to their existing governance paths.

The signal lives under the existing `write_feedback.relations` block and names `schema_memory(operation='infer', subject='relations')`. The ordinary string `next_actions` list receives the same route. No new top-level result key or operation docstring is needed.

### Threshold the existing grouped observations

`infer_relation_registry` exposes a `recurrence_threshold` argument defaulting to three for direct deterministic use. After grouping, every unregistered label whose count meets the threshold is inserted into the copied proposal with `parent: null` and `description: null`. Labels below the threshold remain visible in the evidence list but are not proposed.

### Keep proposal generation response-only

Inference performs no writes. Existing extensions are copied unchanged, and `setdefault` prevents an observed label from overwriting a reviewed definition. The command layer's current explicit-save, expected-hash, and observed-deletion checks remain untouched.

## Risks / Trade-offs

- [A recurring plain label is not yet a valid namespaced extension] -> Preserve the raw evidence exactly and leave namespace, parent, and description for human review rather than inventing semantics.
- [Loading the registry for feedback adds work] -> One small registry load is bounded and the body is already parsed for structural feedback.
- [Making the observation advisory could look like qualification] -> Pin both predicates and an unknown-only write in regression tests; the warning changes severity, not eligibility.
- [A low recurrence threshold creates noisy proposals] -> Default to three while retaining the direct function parameter for deterministic tests and specialist callers.
