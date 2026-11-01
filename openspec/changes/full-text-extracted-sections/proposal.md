# Proposal

## Why

The extraction of a Source was cut to its first 512 KB, so the rest of a long document was never indexed and never readable. A phrase near the end of a large file could not be found, and an agent could not read past the cut.

## What Changes

- Stop cutting extracted text. The Source page keeps the first part. Further parts are pages in a hidden folder beside the stored original, written in the same atomic batch.
- A search hit on a part is one hit on its Source. The hit names the matched part and carries the matched passage with its offset in the whole text.
- `read_memory` returns a long Source's extracted text in bounded windows that walk the whole text in order. A part page reads as its Source.
- Delete, move, reconcile, export, audit and the viewer map treat the hidden folder as part of its Source.
- A paragraph of more than 350 words is indexed as consecutive chunks, so no word is left out of semantic search.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `automatic-media-processing`: keep the whole extraction on part pages and retire parts with the extraction that owns them.
- `attachment-source-ingestion`: one Source page per artifact, with hidden part pages as its text.
- `find-recall-efficiency`: a hit reports its matched part and passage.
- `get-payload-shape`: `read_memory` reads long extracted text in windows.
- `hosted-vault-portability`: export keeps part pages and deletion removes them.

## Impact

Pages written for long extractions have a new shape: new front matter fields on the Source page and hidden part pages. The `read_memory` tool gains an `offset` argument, and search hits gain two fields. Derived tool schemas and documentation are regenerated.
The export archive layout and format do not change. The durability registry gains one rule, so a release before this change cannot restore an archive that holds part pages.
Existing Sources that were cut keep their stored text. Re-extracting them is a separate decision and this change does not do it.
