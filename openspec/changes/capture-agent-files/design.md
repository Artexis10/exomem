## Context

Client artifacts already preserve original bytes. Local CLI attachment currently requires a separate token file.
Compiled supersession rewrites predecessor metadata, which cannot implement immutable Source correction.

## Goals / Non-Goals

**Goals:** Accept original files through existing HTTP MCP authority, reject altered text, and retain auditable Source corrections.
**Non-Goals:** Read arbitrary server paths, start another listener, change folder import, or enable an unimplemented Cloud upload bridge.

## Decisions

Use an opaque one-use ticket in private state. The CLI reads the file and sends bytes through the existing upload route.
Reuse session validation, held handles and artifact preservation. A signed token would still require state to enforce single use.
Keep the existing local-token route. Bind new holds to the complete originating authorization instead of an owner audience alone.

Hash intended bytes before capture. Text means the exact supplied UTF-8 encoding; file means the original artifact.
The rendered Source hash remains the optimistic concurrency hash for correction targets.

Create the successor and a canonical receipt in the existing atomic capture batch.
Key each receipt by the predecessor's immutable identity. Resolve chains through bounded point reads, with independent authorization at each hop.
Keep historical page reads explicit and exact artifact downloads stable. Export receipts as canonical data.

The Cloud gateway implements the same portable handoff fields and tenant/session checks in its separate upload bridge.
Until then, Cloud supports hash-checked client file handles and reports upload handoffs as unavailable.

## Risks / Trade-offs

Upload tickets add one temporary lifecycle and expire without custody if the caller loses the upload reply.
Hash refusals cost the caller a corrected upload; they prevent silently accepting altered originals.
Path confinement can reject a legitimate symlink; the caller must place the original under the selected read root.
Invalid correction chains refuse current reads because they cannot prove which original is current.
Historical reads remain available. Each receipt lookup costs one bounded read per chain hop.

## Verification and release

Prove a synthetic original through handoff, CLI upload, held handle, Source capture and original-byte recovery.
Check hash mismatch, traversal, session substitution, revocation, replay, size bounds and preserved correction history.
Run scoped tests during implementation, then the full corpus through CI for the integrated batch.
An independent security review covers the exact batch before merge.
The normal release owner publishes it and confirms transfer reachability; no production data write is part of this change.
Rollback restores the prior package. New correction receipts remain canonical; a correction is reversed with another corrected Source.
