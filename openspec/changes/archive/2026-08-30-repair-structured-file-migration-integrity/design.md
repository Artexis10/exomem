## Context

Structured-file apply currently moves and re-renders items under one writer boundary, writes an inverse receipt, and records a generic activity entry. It does not advance the collection audit head or replace item audit markers. Inspection therefore compares the migrated container with the pre-migration head and treats moved markers as unmatched. The same real-vault proof also exposed two independent parity bugs: audit reconstruction recognises `rebaseline` but not `plan_rebaseline`, and the structured filename renderer can preview an NFC compatibility character that the held-path mutation seam normalises to a different NFKC spelling.

The repair must keep the existing public preview/apply command shape and the current Records reader floor. It must also preserve the design decision that UUIDs remain immutable internal identity while filenames are readable projections.

## Goals / Non-Goals

**Goals:**

- Make a successful representation migration leave a previously healthy or acknowledged-gap collection healthy at the same continuity level.
- Bind item moves, managed-body changes, item markers, manifest head, activity events, inbound-link rewrites, and inverse metadata to one atomic publication.
- Ensure preview and held publication use the same portable filename spelling.
- Restore Planning/Records parity for rebaseline reconstruction and later revision.
- Prove the repair against realistic copied-vault shapes, including a compatibility-Unicode natural key.

**Non-Goals:**

- Hiding, deleting, or automatically blessing a pre-existing malformed audit chain.
- Removing collection or item UUIDs from canonical frontmatter.
- Automatically renaming items when their natural key changes.
- Changing the public `maintain_memory(mode="structured-files")` request shape.
- Building the customer graph or note-browsing portal.

## Decisions

### Extend the existing version-1 item event chain

Every item whose path or bytes change receives one deterministic ordinary `update`/`plan_update` audit event. Events are ordered by stable item identity and chained from the collection's current head. Each event records the original and final item hashes, the final canonical path, and the logical intermediate manifest/container hashes. The final event becomes the manifest head, and the final item bytes carry their corresponding event markers.

This reuses the already-deployed version-1 reader and avoids inventing a new lifecycle event schema or raising the hosted reader floor. A single synthetic collection-level event was rejected because unchanged item markers would no longer bind their current paths and bytes. Rebaselining after apply was also rejected because a governed migration has continuous provenance and must not manufacture a permanent gap.

Transition IDs are deterministic 24-hex projections over the exact source snapshot, collection/item identity, original path/hash, final path, and pre-marker rendered hash. This lets preview expose truthful final hashes without a plan-ID cycle, while the exact source snapshot and item identity make accidental reuse infeasible.

### Treat current audit health as a migration precondition

Preview reports an audit blocker unless the selected collection is `baseline`, `ok`, or `acknowledged_gap`. Apply rechecks the same state under the writer boundary. A migration may extend an acknowledged checkpoint, but it may not launder an existing fork, missing event, unmatched marker, or ambiguous canonical item.

This deliberately leaves genuinely malformed historical collections for an explicit recovery design. Representation maintenance is not an audit-repair back door.

### Canonicalise structured filename compatibility spelling before planning

The joined natural-key filename input is normalised to NFKC before the existing human filename sanitizer, byte limit, collision key, and suffix logic run. Preview therefore names the exact spelling the held generic-path seam can acquire and publish. The frontmatter value and rendered heading remain unchanged Unicode; only the derived filename uses the portable compatibility spelling.

Changing the generic held-path security boundary was rejected because its NFKC classification protects every vault mutation, while this defect belongs to a projection that promised portable paths.

### Keep one atomic batch and strengthen its inverse

The guarded batch writes final item bytes, the final manifest head, all audit event lines, mutable inbound-link rewrites, and the structured-files receipt after staging moves. The inverse receipt includes the manifest head change in addition to each changed or moved file. Any validation, target-hash, log, or receipt failure rolls staged paths back and leaves the original collection and audit head byte-identical.

### Restore profile-neutral rebaseline handling

Audit reconstruction treats `rebaseline` and `plan_rebaseline` as the same discontinuity shape. Revision accepts `acknowledged_gap` as a valid current checkpoint and subsequent events preserve the recorded discontinuity. No malformed structural gap becomes acknowledgeable through this parity fix.

## Risks / Trade-offs

- **A large collection emits many audit events in one log publication** → Bound the event count by the existing structured-file item cap and publish the complete ordered set in one planned activity write.
- **Deterministic event IDs could collide** → Bind the full exact source/item transformation and refuse duplicate transition identities before publication.
- **Logical intermediate container states never existed as separate filesystem commits** → They are an audit ordering over one atomic transaction; the final event hash must match the only visible committed state, and failure exposes none of the intermediates.
- **NFKC filenames differ from display titles for compatibility characters** → Preserve exact canonical/display text in frontmatter and headings; preview shows the portable filename before apply.
- **Existing malformed collections remain blocked** → Surface the exact audit blocker and handle recovery as a separate, explicitly reviewed operation rather than weakening migration safety.

## Migration Plan

1. Ship the reader/parity fixes and audit-aware structured-file planner/apply together.
2. Run focused lifecycle and migration suites, then repeat the full migration on a fresh Markdown-only copy of the real vault.
3. Require post-apply collection inspection to retain `ok` or `acknowledged_gap`, require a second preview to be empty, and verify rollback on injected publication failure.
4. Deploy the release before applying any live-vault representation migration.
5. Preview and apply one collection at a time; leave collections with pre-existing structural audit gaps untouched for a separate recovery change.

Rollback before live migration is code-only. After a committed migration, the strengthened inverse receipt contains the original and final manifest/item paths and hashes needed for an exact guarded inverse; no rollback guesses a UUID path or prior body.

## Open Questions

None for this repair. Recovery of already malformed historical audit chains is intentionally a separate design decision.
