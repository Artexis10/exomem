# Design

## Context

See `proposal.md` for the source-kind policy. Historical hosted profiles freeze their command descriptors but invoke the current shared capture implementation. Their existing open-string kind arguments support corrected requests.

The terminal UI uses the same product invocation API as programmatic callers. An owner-library principal does not identify which adapter called that API.

## Goals / Non-Goals

**Goals:** Preserve historical descriptor identity while making the runtime migration explicit. Keep human capture available and report unknown counts honestly.

**Non-Goals:** Move existing sources, change hosted transport shapes, classify content heuristically, or add persistent reminder state.

## Decisions

### Bind human capture in the adapter

Bind a request-local capture context in the terminal UI adapter. The shared product API does not infer human involvement from the library principal. Existing hosted web, upload and legacy-import adapters retain their explicit exceptions. A wire argument cannot select an exception.

Inferring human involvement from library authority would also exempt programmatic agents. A separate invocation API would duplicate coercion and authorization without adding a boundary.

### Preserve descriptor identity and migrate runtime behavior

Advance the existing bootstrap operating-contract version. Project one canonical kind rule and migration notice into every current and historical bootstrap profile. Keep historical descriptors, schemas and candidates unchanged. Regenerate current candidate descriptions through their existing generators.

Reuse the canonical `SOURCE_KIND_REQUIRED` envelope. Its corrective guidance states that older optional-kind and `other` instructions no longer govern capture. The refusal precedes effects; a retry uses either existing kind argument.

Changing released descriptions would invalidate their recorded identities. Requiring a profile switch would leave old callers reaching the same changed implementation.

### Keep counts and advisories honest

Use complete visible page counts for known source kinds. Propagate scan failures into unknown counts and explicit read states. Sort complete counts before unknown counts, with kind names breaking ties. Missing folders count as empty only when their absence is established.

Show classification-debt advice only to the verified owner. Omit advisory output if detection cannot establish its count. Advisory failure cannot change a committed capture outcome. Reuse capture counts; add no scan or persistent state for advice.

## Risks / Trade-offs

- Old automation omits a kind and stops capturing. Corrective errors name the required argument; bootstrap identifies the behavioral migration.
- Cached descriptions can cause a refused attempt. The canonical refusal carries the correction even when callers skip bootstrap.
- Filesystem reads can fail. Unknown counts remain visible and do not prevent kind selection.

## Migration Plan

1. Run historical refusal-and-retry journeys; verify correction through both existing kind arguments.
2. Run descriptor pin and bootstrap budget checks; verify immutable history and bounded guidance.
3. Release enforcement, guidance and regenerated current artifacts together; verify capture recovery on the released runtime.
4. If deployment fails, restore the prior runtime image; verify existing sources remain readable at their original paths.

Existing sources need no data rewrite. A runtime rollback restores its previous capture rules; it does not undo captures committed by the new runtime.
