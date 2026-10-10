## Why

A vault present before a server's first start is embedded by a background initial build. If a live write reaches the serving sidecar before that build first plans, the write gives the sidecar the recall encoder's identity. Planning then treats the sidecar as built, and the imported pages are never embedded. Nothing at runtime reconciles them, and recall reports nothing missing.

This reproduces on main. A laptop run of the 0.114.0 Cloud image ended with 18 chunks after 10 minutes and an idle CPU. Friends who import a vault and save a note in their first minute would lose semantic recall over their imported notes without being told.

## What Changes

- **The initial build is due by coverage.** It is due whenever no active pointer is published and the serving sidecar does not cover every eligible page, whether or not an earlier build left a shadow sidecar.
- **Held vectors are copied, not re-encoded.** When the serving sidecar is in the build's own space, the build copies each vector it already holds for the same text of the same page, and encodes only the rest. A legacy sidecar with a few stale pages therefore costs only those pages.
- **A published pointer still means no startup scan.** While no pointer is published, planning walks the vault until it finds the first uncovered page.

## Capabilities

### Modified Capabilities

- `multilingual-recall`: the initial-build planning rule and the startup-scan scenario.

## Impact

- `src/exomem/recall_migration.py` and `tests/test_embedding_migration.py`.
- Personal servers whose serving sidecar is the unpublished legacy `.embeddings.sqlite` now scan coverage at each start. On drift, they copy into a published space sidecar once, encoding only the stale pages, and later starts scan nothing. A legacy sidecar that already covers the vault stays unpublished, and its coverage is scanned at each start; a review measured about 3 s per start for 3,000 pages. Cloud cells whose sidecar grew only through live writes scan the same way until their build publishes.
- A batch made only of pages that lost every unit no longer fails the build at every start.
