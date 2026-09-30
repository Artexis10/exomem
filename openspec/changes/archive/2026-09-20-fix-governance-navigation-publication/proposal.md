## Why

A freshly enrolled vault can validate a capture and then refuse its commit with
`GOVERNANCE_CATALOG_PUBLICATION_BLOCKED`. Migration omits existing navigation and log
pages from its catalog, although ordinary capture updates those pages in the same
governed batch as the new note.

## What Changes

- Include navigation Markdown in new migration catalogs.
- Preserve navigation exclusion before projected candidate scoring, graph traversal,
  reranking and continuation.
- Let exact guarded navigation writes repair missing catalog entries in already
  migrated vaults through the ordinary successor-generation publication.
- Preserve predecessor checks for every existing catalog row and every non-navigation
  mutation; raw catalog mutations cannot invoke the compatibility repair.
- Exercise fresh hosted enrollment and legacy catalogs through real capture writes.

## Capabilities

### Modified Capabilities

- `governance-kernel`: catalog completeness for navigation and tightly scoped repair
  of navigation rows omitted by earlier migration.

## Impact

Changes the schema migration inventory, planned-write catalog preparation and
projected candidate selection. It preserves ordinary recall's navigation exclusion,
the public tool contract, policy
membership, canonical file guards, or immutable historical catalog generations.
