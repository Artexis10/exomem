## Why

The operator authorized moving and categorizing the 223 files in the legacy `Sources/Other` folder. Reclassification cannot do that safely today.

- `move_file` refuses a move with `APPEND_ONLY` when an append-only page links the Source. The link rewriter also counts `[[stem]]`, `[[stem|alias]]` and `[[stem#heading]]` links as changed when it returns identical text, so most of these refusals are spurious. Reclassification keeps the basename, so stem and title links survive a move unchanged.
- A move is not reversible. Reclassifying a file back into `other` is refused, and only the latest prior path survives, as an unread scalar. The prior kind and domain are never recorded.
- The preview does not say which referrers stay unchanged or would dangle.

Only a link that names the Source by its full or knowledge-base-relative path, inside an append-only page, needs history to keep resolving. Nobody knows yet how many of the 223 files have such a referrer. That count decides whether history-aware reads are needed at all.

## What Changes

Phase 1 makes the drain safe and reversible:

- The link rewriter reports a referrer as changed only when its bytes change. A stem or title link in an append-only page no longer refuses the move.
- Reclassification appends each prior classification (path, kind and domain) to a `reclassified_from` list in canonical frontmatter. A legacy scalar reads as one entry with an unknown classification. Fields are written by parsed-span patches instead of the current regex setter, and body bytes are proven equal before commit.
- A revert mode restores the latest recorded classification, including `other`, and removes that entry. A legacy entry with an unknown classification cannot be reverted automatically.
- The preview reports, per file: destination, mutable referrers to rewrite, append-only referrers left unchanged by link form, and any refusal.
- A Source whose append-only referrers name it by path still refuses to move. The preview lists it as needing history resolution.

Phase 2 starts only if the Phase 1 preview of the live drain counts at least one such Source. It adds history-aware resolution for read-only commands. Its decisions stay open until that count exists; the design records the constraints that bind them.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `command-surface`: reclassification ignores unchanged referrers, records a reversible classification history, offers revert, and previews every referrer outcome.

## Impact

- Code: `vault.rewrite_wikilinks_for_move` change counting, `move_file.py` append-only checks and `_repoint_artifact`, `reclassify_source.py`.
- Data: no vault page changes until a reclassification runs. Existing `reclassified_from` scalars stay readable.
- Operations: the live drain follows release and installation on the personal service, with a preview first.
