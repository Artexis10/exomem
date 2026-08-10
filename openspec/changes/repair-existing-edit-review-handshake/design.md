## Context

Existing-page semantic writes hash the exact before and after Markdown states into an opaque transition token. Since note timestamps became second-granular, a logical edit that is validated in one request and committed in another must reuse the validated `updated:` instant or the second render produces different bytes. Release 0.40 pinned that instant in the shared `edit.py` path, but the separate frontmatter and batch writers still read the clock again. The fill-row variant exposes no validation path at all, so a stale relation disposition can hard-lock that route.

The relation-review recovery contract has a second problem: existing validation returns only `transition_hash`, while clients are told to supply `relation_review_hash`, and validation rejects review intent when that unknown value is absent. Remediation and documentation also mix creation-only draft fields into existing-edit guidance.

## Goals / Non-Goals

**Goals:**

- Make every advertised `edit_memory` kind deterministic across validate and commit.
- Ensure the after hash always identifies the literal bytes handed to the transactional commit.
- Make the reviewed-none round-trip self-describing and usable by a generic MCP client.
- Give every edit kind, including fill-row, the same recovery path from a stale disposition.
- Keep error and documentation surfaces aligned with the actual schema and parser.

**Non-Goals:**

- Do not weaken relation governance or downgrade stale dispositions to warnings.
- Do not accept Dataview `relation:: target` syntax; canonical note-level relation bullets remain the authoring form.
- Do not change the relation fingerprint algorithm or introduce a migration.
- Do not add a dedicated disposition-refresh mutation in this repair.

## Decisions

### Freeze one reviewed render stamp for every existing-page writer

Move the bounded transition-token stamp reuse policy into `semantic_writes`, the owner of existing transition tokens. Every edit leaf asks that shared helper for its effective render stamp before constructing frontmatter. Validation mints the token with that stamp; commit decodes and reuses it. The same literal `after_source` value is both hashed by `preflight_existing` and handed to `commit_existing`.

This extends the proven 0.40 behavior rather than excluding `updated:` from the transition hash. Excluding a server-owned field would make the token stop describing the exact committed page. Freezing a timestamp outside the token was also rejected because retries would need another state store and could not remain self-contained.

The existing age and future-skew bounds remain fail-safe: malformed, too-old, or implausibly future stamps are ignored, causing exact-state validation to fail instead of writing a dishonest timestamp.

### Return explicit before, after, and review hashes without removing the legacy name

Existing validation adds direct `before_hash`, `after_hash`, and `relation_review_hash` fields. `relation_review_hash` equals the token fingerprint currently exposed as `transition_hash`; the old key remains as a compatibility alias. Tests compare `after_hash` to the hash of bytes actually committed.

### Validation checks review intent but supplies the fingerprint

`preflight_existing` receives whether the call is validation-only. In that mode it validates disposition, reason, stable identity, token, and all other semantic rules, but it does not require or compare the caller's review hash. It returns the canonical `relation_review_hash`. A commit still requires an exact match, preserving the reviewed-transition binding.

This is preferred over a special preliminary call because one deterministic validate response teaches the exact next call and avoids circular validation.

### Give fill-row the same semantic preview contract

The fill-row leaf will render its proposed row and `updated:` frontmatter without writing, pass those bytes through `preflight_existing`, and return the same semantic validation envelope as other edit kinds. Commit reuses the shared edit commit seam and all supplied transition/review guards.

### Make remediation route-specific and documentation concrete

Relation-disposition findings distinguish creation from existing-page transitions. Existing edits name `transition_token` and the returned `relation_review_hash`; creation writers name their draft fields. Tests compare referenced parameter names against the relevant public schema.

Full bootstrap and `edit_memory` documentation show both calls in the review round-trip and the accepted body syntax:

```markdown
## Relations
- supports [[Knowledge Base/Notes/Research/example-target]]
```

They explicitly state that Dataview `supports:: [[...]]` is not a typed relation.

### Preserve semantic error identity without inventing missing arguments

Semantic failures keep their code and reason when adapted to `EditError`, but carry an empty missing-field list. The command adapter appends `(missing: ...)` only for actual argument-validation failures.

## Risks / Trade-offs

- **Old transition tokens without a stamp cannot survive a clock tick** → Keep current bounded fallback behavior; clients revalidate once after upgrade.
- **More response fields add payload bytes** → Add only three scalar hashes and retain bounded semantic feedback.
- **Fill-row validation adds another code path that could drift** → Reuse its existing row renderer and the shared commit renderer, with byte-hash conformance tests.
- **Route-specific remediation can drift from schemas** → Add schema-conformance tests that extract every parameter named by the remediation.

## Migration Plan

Ship as a backward-compatible patch release. Existing clients may continue using `transition_hash`; updated clients use `relation_review_hash`. Deploy the release, confirm bootstrap reports the new version, then rerun the Aberdeen validate/commit reproduction through the public connector. Rollback is code-only; no vault or database migration is performed.

## Open Questions

None. The acceptance criteria settle the review semantics, exact-byte invariant, documentation form, and error behavior.
