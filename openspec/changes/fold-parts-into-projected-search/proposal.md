# Proposal

## Why

An enrolled vault serves search from its governed catalog, and that catalog leaves out the part pages of a long Source.
Projected search therefore finds text only in the first part of a long extraction, while direct search finds it anywhere.

## What Changes

- Store the text of each further part on its Source's full-text catalog row, never as a catalog item.
- Search part text as text of that row: one hit on the Source, with `matched_part` and a `passage` that opens a `read_memory` window.
- Republish a Source when its parts change: a scope change, a move, a trash and recovery, a new extraction or a rebuilt part.
- Count each part against the catalog capacity, and log the parts that a full catalog leaves out.
- Raise the projector version, so that the startup refresh adds parts to an existing catalog. The refresh reuses each stored vector whose projection text is unchanged.

## Capabilities

### Modified Capabilities

- `governance-kernel`: A governed catalog searches a long Source in full.
- `attachment-source-ingestion`: A governed catalog holds part text only as content of its Source's row.

## Impact

The change affects catalog publication, the projection store (schema version 3, which still reads version 2), projected retrieval and the projected find wire. Direct search, authorization and content hashes keep their contracts.
