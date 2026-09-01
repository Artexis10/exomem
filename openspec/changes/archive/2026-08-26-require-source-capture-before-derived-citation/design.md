## Context

Compiled-note writers currently normalize explicit `sources` and may commit even when an entry cannot be resolved, returning only a warning. That supports a compile-then-capture order, but it also permits a durable derived claim to outlive the raw material it says supports it. An external connector message ID or file ID can then look like a vault citation even though no governed source exists.

The system already has the right write architecture: semantic precommit validation, stable governed references, a shared authorization-aware resolver, atomic multi-file batches, append-only Source/Evidence zones, and source back-references such as `ingested_into`. This change moves source closure into that shared precommit boundary and adds a legacy audit; it does not add a new ingestion system.

## Goals / Non-Goals

**Goals:**

- Ensure every non-empty explicit source citation on a newly created or changed derived note resolves to governed captured material before commit.
- Make capture-first ordering consistent across all semantic writers and generated product surfaces.
- Preserve connector identifiers and URLs as provenance on captured material rather than accepting them as pseudo-links.
- Keep honest `sources: []` valid.
- Surface legacy unresolved citations deterministically without mutating or fabricating evidence.
- Maintain source back-references atomically with the derived write.

**Non-Goals:**

- Automatically fetching email, Drive, web, or other connector content.
- Reconstructing missing raw material from excerpts, scripts, summaries, or other derivatives.
- Requiring an external source for every conclusion.
- Changing the epistemic meaning, authority, or confidence of compiled notes.
- Blocking unrelated edits to legacy notes solely because they already contain an unresolved citation.
- Turning legacy source debt into a default unsolicited attention queue.

## Decisions

### 1. Source closure is one shared precommit invariant

A shared `validate_source_closure` leaf receives the final compiled-note frontmatter, prior frontmatter when editing, caller authorization context, and the planned write batch. It normalizes every non-empty `sources` entry, resolves it through the governed reference resolver, authorizes the target before returning any identity, and confirms that the target is captured Source or Evidence material eligible under the existing provenance contract.

Creation and complete replacement validate every explicit entry. Patch editing validates when the operation adds, replaces, or otherwise changes `sources`; an unrelated body or metadata edit to a legacy page does not reassert the existing unresolved claim and remains allowed. An operation that makes a compiled page active while introducing source claims validates those claims before semantic-unit checks publish anything.

The check occurs inside writer authority immediately before publication, after argument validation but before any Markdown or auxiliary state is written. This is preferable to a command-wrapper check because all facades and future semantic writers inherit it automatically.

### 2. Empty and absent sources are valid; unresolved sources are not

`sources: []` and an absent source list both mean that no external source is asserted. They are valid final states when the note is original, experiential, or otherwise has no external captured material. A non-empty list is an explicit provenance claim and every entry must close.

This distinction avoids forcing performative citations while preventing a broken link from being treated as honest absence.

### 3. External identifiers live on the captured material

Connector message IDs, Drive file IDs, URLs, and similar remote identifiers are provenance metadata on a captured Source or Evidence page. They are not accepted as `sources` values merely because they are syntactically link-like. The derived note cites the governed page or its stable reference.

The capture operation remains responsible for preserving raw bytes/text and origin metadata. The compiled writer neither contacts external systems nor converts an identifier into content. This keeps the server a pure substrate and makes connector availability irrelevant to note validation.

### 4. Refusal is bounded and non-disclosing

Failure uses stable code `UNRESOLVED_SOURCE_CITATION`. The application envelope returns a bounded list of the caller-supplied unresolved entries, a total count, and remediation to capture the material and retry the unchanged derived write. It never returns a candidate path, hidden target, authorization distinction, or corpus search result. Missing, malformed, ambiguous, ineligible, and withheld targets share the same public classification where distinguishing them would disclose state.

The normalized mutation and source set participate in idempotency. A refused attempt writes no receipt that can be replayed as committed. After capture, retrying the same derived write under the normal idempotency contract can commit.

### 5. Derived write and source back-reference publish atomically

Once every target is resolved and authorized, the writer adds or updates each supported source back-reference in the same atomic batch as the compiled note. It then rechecks the target versions before publication. A concurrent source move, change, or disappearance produces a stale refusal and neither side is written.

Stable references remain canonical across source relocation. Human-readable wikilinks are rendering details and are normalized to current governed paths during the write.

An alternative was to commit the note and enqueue back-reference repair. That recreates the same interval in which a derived note claims unavailable provenance, so it is rejected.

### 6. Legacy debt is audited, not silently upgraded

`review_memory(mode="audit")` gains category `unresolved_source_citation`. It parses explicit source fields on governed compiled pages, resolves them under the normal audit authority, and emits deterministic findings containing the derived page anchor, bounded unresolved values, and remediation. A source-field link is classified here instead of also producing a generic `broken_wikilink` finding.

The category is available when requested and in all-category audit, but is not registered as a default Epistemic Inbox family. Existing notes remain readable. The finding clears only when the original material is captured and the citation points to it, or when a user explicitly removes the unsupported citation.

No remediation copies content from the derivative into Sources or Evidence. If the original cannot be recovered, the system preserves that fact as unresolved debt until the unsupported claim is explicitly removed.

### 7. Every public writer shares the same contract

The semantic leaves used by `remember_memory`, `replace_memory`, `edit_memory`, and governed Tier-2 compiled-note creation call the shared validator. Registry-generated MCP, CLI, REST, OpenAPI, bootstrap guidance, and schema fixtures expose the same refusal and capture-first remediation. Capture commands themselves are unaffected because they create the material that source closure requires.

No model or optional heavy dependency is needed; the feature consists only of parsing, normalization, authorization-aware resolution, and guarded writes.

## Risks / Trade-offs

- **A formerly warning-only write now refuses** → Mark the change breaking, give one stable remediation, and allow empty sources when no citation is intended.
- **Legacy notes could become impossible to maintain** → Grandfather unchanged unresolved claims for unrelated edits and surface them through audit instead.
- **Resolution could leak hidden source existence** → Authorize before resolution details and collapse missing/withheld/ineligible outcomes into one public error.
- **Concurrent source mutation could tear back-references** → Recheck source versions and publish source plus derived changes in one guarded batch.
- **Existing generic broken-link audits could duplicate findings** → Route source-field failures exclusively to the dedicated audit category.
- **Users may be tempted to recreate raw evidence from a derivative** → The refusal and audit remediation explicitly require original capture or citation removal; no automatic backfill path exists.
- **Tier-2 callers may bypass semantic leaves** → Put the invariant in the shared compiled-write precommit boundary and add cross-surface parity tests.

## Migration Plan

1. Add the shared closure validator, stable error, and cross-writer tests behind the current semantic write boundary.
2. Add the read-only legacy audit category and measure existing unresolved citations without changing files.
3. Enable refusal for new, replacement, and source-changing writes; leave unrelated legacy edits compatible.
4. Recover and ingest original external material explicitly where available, then update derived citations through guarded writes.
5. Re-run the audit and preserve any genuinely unrecoverable items as visible debt until a user removes the unsupported citation.

Rollback disables the new precommit refusal while leaving captured Sources, Evidence, and repaired citations intact. The audit is read-only and requires no data rollback.

## Open Questions

None. The accepted contract is capture first, explicit empty sources when appropriate, and no evidence reconstruction.
