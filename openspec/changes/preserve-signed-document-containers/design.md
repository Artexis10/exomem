# Design

## Context

See proposal.md for the outcome. Existing custody writers stage original bytes and companions, then publish one guarded batch. Existing archive-member storage deliberately replaces ZIP bytes with member blobs and cannot represent an unchanged signed container.

DOCX and PDF extraction, complete extraction parts, job retries and governed original downloads already exist. This change extends those paths and the close-memory-loop original-versus-reading contract.

## Decisions

### Plan payload custody at the existing write boundary

A shared document-container helper inspects staged bytes and returns payload streams, provenance and planned writes. Both Source and Evidence writers use it. The original and initial payloads publish atomically through the existing mutation boundary. Extraction stays in the existing media worker.

This adds one maintained format adapter and a portable provenance binding. It avoids another store, worker or provider bridge. Existing archive-member validation is the nearest reusable component; expose its bounded ZIP checks rather than copying them.

### Inherit authorization through a validated container binding

Each payload companion binds its deterministic location, exact parent path and hash, and member path. Governance resolves the current parent restrictions before disclosing child content. Extraction parts inherit their payload's access through the existing part mechanism. Raw protection covers the entire child subtree.

The existing parent_media field describes timestamped scene frames and cannot carry this contract. A new validated container binding is required. Refusal prevents document disclosure; a false refusal costs that caller access until the missing or invalid custody binding is repaired. No human approval is added.

Resolve staged parent evidence from the planned batch during catalog preflight. At disclosure, check current parent dependencies before cached or projected admission. Include binary downloads under path-only policies. Unchanged child bytes cannot prove that a parent still permits access.

Check selected projected candidates before their text reaches a scorer or changes visible graph results. Apply the same parent check to held Markdown and path-only snapshots.

Keep cached lexical selection before current-parent reads. Validate only the existing retained primary and graph windows; do not scan the namespace for parent proofs.

An evicted parent retains its verified stored identity. Authorize that binary through existing path-only admission and authorize its current companion separately, without hydrating it.

Require a valid binding throughout the reserved payload subtree. Outside it, use the existing tolerant frontmatter parser to detect a parsed container_member claim, then validate that claim strictly. This also checks retained provenance after a desktop move. Preserve ordinary path classification for malformed notes without a parsed claim; this binding does not add an ancestry registry.

### Reuse byte proofs within one request

Repeated child checks must not hash their shared container again each time. Reuse the existing request-owned companion proof map and its file-version checks. This adds no global cache, time limit or disk copy.

Recheck each proof's root, path and current file version before reuse. Read current parent metadata and evaluate parent policy on every admission. Keep the existing restricted final recheck: it cannot capture a new proof. Close held files after capture; the request retains only their identity and digest metadata.

### Separate inspection, custody and verification

Use the standard ZIP reader with the shared path, entry and directory checks. Add aggregate expanded-byte and streaming limits for document inspection. Treat archive syntax and format identifiers as closed protocol data, with reasons beside new lists or regexes. Do not parse XML with regexes or fetch relationships, certificates or external resources.

Unsafe or corrupt inspection leaves the exact container as opaque custody and publishes no children. Unsupported payloads remain explicit. Signature status stays not_verified; this delivery adds no cryptographic trust policy or verification service.

### Keep duplicate preservation unchanged

An already_stored preservation remains free of writes. Initial payloads belong to the original custody batch. Extend the existing explicit media process/retry route for older opaque containers and missing payload recovery; it uses the same helper and guarded writers. Reconcile preserves the provenance block when rendering a payload companion.

Share the lower child-pair planner instead of recursively calling the public custody writers. Explicit processing creates missing children and reuses exact validated matches. A guarded repair may restore missing provenance for exact bytes; conflicting bytes or bindings remain conflicts.

## Proof and release

Prove the public preservation-to-search-to-original-download workflow with neutral synthetic containers and isolated state. Cover DOCX, PDF when its optional engine is installed, long text, duplicate/replay, processing failure/retry, malformed containers and hostile archive inputs. Prove that denial of a parent also denies payload search, text reads, parts and downloads.

Run scoped custody, media, retrieval and governance tests first, then required full CI at the integrated delivery boundary. One independent adversarial review covers the exact batch, archive security, authorization, stored bytes, C4/C6 and product-versus-proof weight. Fresh verification runs the highest-level workflow on delivered bytes.

Deliver one reviewed PR under standing project authority, merge after CI, and use the ordinary qualified release procedure. The previous qualified release is the code rollback; preserved immutable originals are retained. Installed personal-service acceptance uses authorized normal APIs and requires no reflexive restart or out-of-process drain. Publication does not prove installation. Private recovery proof stays in governed Evidence, outside the repository.

## Risks / Trade-offs

- A child binding crosses an access boundary. Validate its location and parent identity, and test later parent restrictions.
- Archive expansion can exhaust resources. Validate before publication and enforce streaming and aggregate limits.
- Optional engines can be missing. Keep custody complete and retain existing actionable processing states.
- Signature presence can mislead callers. Keep verification status explicit and separate from inspection success.
