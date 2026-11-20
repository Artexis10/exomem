## Why

Cell discovery reports `recall_space.state: "unavailable"` both when the cell has no published recall space and when discovery could not read the store at that moment. Discovery read the store with a zero wait on its lock, so concurrent discovery calls on an idle cell reported a published space as unavailable most of the time. A client cannot tell an empty cell from a busy one. A client that trusts the reply may treat a working cell as unindexed.

## What Changes

- Discovery reports three recall-space states: `observed` with its id, `unavailable` when the store holds no published space, and `unknown` when discovery could not read the store.
- Discovery waits a short, bounded time for the store lock, so concurrent discovery calls queue instead of failing.
- The response shape does not change: `recall_space` keeps its `state` and `id` fields, and `id` is null for `unavailable` and `unknown`.

## Capabilities

### Modified Capabilities

- `cell-service-identity`: discovery separates an unread recall space from an unpublished one.

## Impact

- `src/exomem/embedding_index.py`: the published-identity reader raises a distinct error when it cannot read the store.
- `src/exomem/cell_identity.py`: discovery maps that error to `unknown`.
- Clients that compare the recall-space id are unaffected. A client that read `unavailable` as "busy" must now read `unknown`.
