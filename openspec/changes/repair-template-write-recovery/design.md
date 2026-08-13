## Context

The incident crossed four existing seams. `edit_memory` parses every target as a frontmatter-backed compiled page before it dispatches the requested edit. Existing-page semantic validation emits `transition_token`, while tier-2 overwrite commit routes that same value through the creation-shaped `draft_token` parameter. The off-boundary graph coordinator projects arbitrary builder exceptions into `GRAPH_SYNC_REBUILD_STOPPED` without retaining their cause, and ordinary reconcile intentionally refuses ambiguous epoch lineage. Finally, the semantic-isolation census has a POSIX descriptor binder but maps every non-POSIX platform to `sidecar_unreadable`.

Canonical Markdown is authoritative. Graph, lexical, vector, claim, deferred, and CLIP stores are derived. Existing append-only, supersession, transition-review, no-follow, mutation-boundary, and exact-publication guarantees remain in force.

## Goals / Non-Goals

**Goals:**

- Make supported ordinary Markdown editable through the normal body-editing surface without inventing metadata.
- Make tier-2 overwrite validation self-describing while retaining old clients and tokens.
- Preserve graph builder evidence and give callers a recovery action that is valid for the observed epoch state.
- Offer an explicit, derived-only escape hatch when graph lineage is too ambiguous for ordinary reconcile.
- Give Windows the same truthful semantic-sidecar census classifications as POSIX without following aliases or racing replacement.

**Non-Goals:**

- Do not relax frontmatter requirements for compiled-note metadata, tag replacement, fill-row, or frontmatter patching.
- Do not unify every semantic token protocol or repurpose append-only `semantic_transition_token` in this repair.
- Do not auto-quarantine ambiguous graph lineage during an ordinary write or ordinary reconcile.
- Do not change canonical Markdown, activity history, semantic policy, graph query shapes, or optional-model behavior.
- Do not claim recovery of an unproven canonical mutation outcome; the new graph reset applies only to derived state after canonical outcome is known.

## Decisions

### Model frontmatter presence explicitly in the shared editor

`load_editable` will accept an operation-owned `allow_frontmatterless` policy and return whether delimiters were present. Frontmatter-backed pages keep the current exact render: update the server-owned `updated:` stamp, optionally replace tags, then render YAML plus body. Frontmatter-less pages expose the entire normalized Markdown text as the body and render only that edited body with one trailing newline. The normal body, surgical string, batch-string, and section routes opt in only when no frontmatter-dependent sibling operation is requested.

This is preferred over synthesizing YAML because templates are ordinary scaffolds and adding metadata is an unrelated mutation. It is preferred over a template-path special case because ordinary editable Markdown is a data shape, not one hard-coded directory. The shared renderer will be used by both `edit` and `multi_edit` so validation and commit hash identical bytes.

When a frontmatter-dependent operation targets a frontmatter-less page, the adapter retains a stable frontmatter-required failure but supplies an empty missing-field list. A resolved path is never labelled missing.

### Add a response alias at the tier-2 overwrite boundary

`ExistingPreflight.as_dict()` will add `draft_token` only when `operation == "tier2_overwrite"`, with bytes identical to the existing `transition_token`. Commit continues accepting `draft_token`; `transition_token` remains in the response for compatibility; `semantic_transition_token` remains append-only. Schema descriptions and product guidance will show the exact `validate response draft_token -> commit request draft_token` replay.

This is smaller and safer than accepting a new neutral request field across every writer or broadening the append token's meaning.

### Separate graph diagnostics, routine recovery, and explicit lineage reset

The graph coordinator will log the original builder exception with checkpoint identity and retain it as the chained cause of `GraphRebuildStopped`. Public terminals remain content-free: they expose only stable code, checkpoint digest, and remediation.

Terminal remediation will inspect the post-failure epoch classification. `current` or `recovery_required` directs the caller to retry the same mutation identity or run ordinary reconcile. `unavailable` states name the explicit reviewed action: `maintain_memory(mode="reconcile", dry_run=false, rebuild_graph=true)`. Ordinary reconcile continues to repair only proven recoverable drift.

The new `rebuild_graph` flag is valid only for reconcile. In dry-run mode it reports whether an unavailable lineage reset would occur and lists no user paths. In write mode it is effective only when epoch classification is `unavailable`: under the existing mutation boundary it validates and atomically moves the exact live graph set (`.graph.sqlite` plus SQLite companions) and graph floor/checkpoint artifacts to one hidden bounded quarantine directory. Reparse points, non-regular entries, path changes, or an open Windows reader fail before reset. It then starts the existing full canonical rebuild protocol from a clean legacy derived state. Canonical Markdown, receipts proving canonical outcomes, logs, non-graph sidecars, and user-visible paths are never moved.

Quarantine uses reversible same-filesystem renames. A partial quarantine is rolled back in reverse order. Once every live name is isolated, a later rebuild failure leaves the old set quarantined rather than mixing old lineage with a new sidecar; the response names the retained quarantine identifier. Successful publication removes only that operation's quarantined derived files on a best-effort basis.

This explicit action is preferred over making reconcile guess through contradictory floor/checkpoint/acknowledgement state. It is also preferred over deleting derived files because quarantine preserves operator evidence and permits rollback before the clean rebuild begins.

### Add a native Windows retained-handle sidecar binding

The POSIX binder remains unchanged. On Windows, audit will reuse the existing `CreateFileW(..., FILE_FLAG_OPEN_REPARSE_POINT)` and file-identity primitives to retain no-delete-share handles for the vault root, Knowledge Base directory, live sidecar, and present SQLite companions. SQLite reads or exact-row repairs still use the canonical path, but the retained handles prevent replacement and the binder compares stable file identities before accepting a repair. Handles close in all paths.

Binder outcomes become `absent`, `regular`, `unreadable`, or `unsupported`; census output maps only actual open/query failures to `sidecar_unreadable`. Platform inability is reported as `sidecar_unsupported`, never as corruption. This is preferred over raw `Path.stat()` because it would reopen the time-of-check/time-of-use and reparse races that the POSIX descriptor design closed.

## Risks / Trade-offs

- **A generic ordinary-Markdown editor could bypass compiled-note lifecycle rules** -> Only body-only operations opt in; semantic preflight still evaluates the complete candidate and compiled pages keep their existing contract.
- **The token alias adds one response field** -> Keep both fields byte-identical and add public schema round-trip tests; remove neither existing field.
- **Explicit graph reset can discard useful derived diagnostics** -> Require the opt-in flag, quarantine rather than delete, expose a bounded identifier, and never touch canonical files or canonical-outcome receipts.
- **Windows handle sharing could block legitimate sidecar publication** -> Census calls are bounded and always close handles; tests cover successful publication after release and open-reader refusal while held.
- **Original graph exceptions may contain paths or content** -> Record them only in service logs; public responses retain the existing closed content-free projection.
- **Native Windows ACL/service behavior is environment-sensitive** -> Add seam tests on all platforms and native Windows handle tests; preserve the existing elevated LocalSystem acceptance boundary rather than simulating it as complete.

## Migration Plan

Ship as a backward-compatible patch. No stored token, Markdown, or database migration is required. Existing callers continue using `transition_token` responses and `draft_token` requests; updated callers can copy `draft_token` by name. Existing reconcile calls remain non-destructive. After deployment, rerun the original template edit, overwrite validate/commit, injected graph-builder failure, explicit unavailable-lineage recovery, and native Windows sidecar census through the installed product surface.

Rollback is code-only unless an operator explicitly used `rebuild_graph=true`. In that case canonical Markdown is still unchanged; any retained quarantine directory remains derived evidence and the prior release can rebuild its own graph normally.

## Open Questions

None. The approved product boundary is automatic ordinary reconciliation plus explicit opt-in quarantine only for ambiguous derived graph lineage.
