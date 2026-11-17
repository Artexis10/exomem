## Why

The cell embedded each declared passage with the Source title in front of it.
A passage score therefore changed with the title that the caller chose for the Source.
A caller that maps scores to its own scale had to send one constant title for every Source.

## What Changes

- The cell embeds a declared passage exactly as the caller sent it, with nothing in front of it.
- A caller that wants context in the embedding puts that context in the passage text.
- Sources without declared passages keep their title in the embedded text, as before.

## Capabilities

### Modified Capabilities

- `passage-evidence`: a declared passage's embedded text is exactly its declared text.

## Impact

The change touches the declared-passage chunker only.
A cell that embedded declared Sources under an earlier build keeps their old rows, because incremental indexing skips an unchanged page.
Passage search withholds those chunks as stale until an embedding rebuild or a fresh capture replaces them.
