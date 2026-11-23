## Why

An upload commit ran its derived fan-out (lexical, resolver, graph and embedding refresh) while it still held the vault mutation boundary. The hold therefore grew with the cost of that fan-out, and every note save behind it waited. On the personal service, 17 `upload_preserve` holds measured a p50 of 33.7 s, a p95 of 70.7 s and a maximum of 186 s. An earlier change already moved the byte copy out of the hold, so the fan-out is the remaining cost that grows with the corpus.

## What Changes

- The upload doors (Evidence upload, Source capture, Evidence and Source upload sessions, and archive manifests) commit with the post-commit fan-out off. They hold the boundary only to plan, install the stage, and write the pages, navigation and graph epoch.
- Before the install, the commit records durable full-refresh custody for its Markdown pages. A stop between the install and the fan-out leaves that custody, and the next drain refreshes the pages.
- The fan-out runs after the release and before the door responds. When it completes, it retires its own custody by exact revision.
- The fan-out reads the committed pages from disk and applies their current recall admission, as every other deferred fan-out does.
- Each upload receipt gains a `derived_sync` field: `completed` when this request's fan-out finished every component, otherwise `pending`. A replayed or already-stored receipt says `pending`, because that request ran no fan-out.
- The upload doors skip the near-duplicate and contradiction advisory pass. Their receipts carry no advisories, so the pass cost time and returned nothing.
- A store-mode vault records no file-mode custody, because its commit derives its own rows. A request that already holds fast-acknowledgement custody records none either.

## Capabilities

### Modified Capabilities

- `hosted-mutation-safety`: an upload commit holds the vault mutation boundary only to plan and install, and runs its derived fan-out after the release.

## Impact

- Code: `writer_lease.py` (`fanout_after_release`), `add.py`, `preserve.py`, `archive_members.py`, `server_transfer.py`.
- Behaviour: a note save no longer waits behind an upload's derived fan-out. The upload response still waits for the fan-out, so a client sees no change except the new `derived_sync` receipt field.
- Undo: revert the change. No stored file format changes. Custody left by a stopped request is ordinary full-refresh work that any later drain retires.
