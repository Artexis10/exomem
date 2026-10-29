# Design

## Context

See proposal.md for the problem. The existing operation owns parsed frontmatter patches, an undo stack, and guarded relocation.
Compiled interpretation history excludes Sources; collection history owns a different record model.

## Decisions

- Keep `reclassified_from` as the undo stack. Add `classification_history` with `events`, retained-only coverage, and optional `legacy` metadata.
- Each event records its operation, UTC timestamp, accepted reason, and before/after path, kind, and domain.
- Preserve all known legacy correction fields once, including the old undo stack. Missing reasons remain unavailable and unattributed.
- Append events for unchanged classifications and reversals. Never truncate events or remove them during undo.
- Patch the journal through the existing parsed frontmatter writer and transaction. Generate the timestamp once before the declared transform.
- Retain the original read guard for in-place writes. Reject a relocation transform whose input differs from the planned source snapshot.
- Bind the renamed source bytes to the destination write's expected hash. Existing rollback preserves an edit that arrives after preflight.
- Read the journal through existing released frontmatter. Keep operation-log history and compiled revision history separate.

## Risks and controls

The snapshot check prevents overwriting an external metadata edit. A concurrent edit costs the caller one fresh read and retry.
Malformed journal structure refuses the correction instead of discarding retained history; the owner must repair the malformed metadata.
The journal grows with explicit corrections, without duplicating captured media or introducing another history store.
Historical addresses remain subject to the existing reference checks; events do not create path aliases.

## Delivery

Ship the additive format through the ordinary package release. No source migration or installation is implicit.
An older writer cannot maintain the journal, so rollback must suspend classification writes until a compatible writer returns.
Use generated media in committed tests and runtime-only copies for private acceptance evidence.
