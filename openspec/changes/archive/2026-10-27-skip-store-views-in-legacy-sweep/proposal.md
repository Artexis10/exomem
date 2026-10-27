# Proposal

## Why

Collectionless Records inventory sweeps every Markdown file under the Records layer for legacy trackers on each call. When a collection store routes the Records collections, most of those files are item views that the store itself published. The sweep authorized each view and parsed its front matter, although no store view is a legacy tracker. On a sandbox copy of a real vault, 408 of the Markdown files under Records were store views, and records list took about 2.1 s.

## What Changes

- When a collection store is bound, inventory reads the exact paths of the views that the store owns, in one snapshot, and the legacy-tracker sweep skips those paths before it authorizes or parses them.
- Every other Markdown file under the Records layer keeps its current discovery, authorization and governance filtering. This includes a file beside a collection's manifest and a file in an item directory that is not a published view.
- Collections under file authority keep their current sweep.
- An item view whose own front matter carries a tracker type is no longer reported as a legacy tracker.

## Capabilities

### Modified Capabilities

- `records`: The inventory requirement states that store item views are not legacy-tracker candidates.

## Impact

The change touches `discover_legacy_trackers`, its caller `record_governance.inventory_collections`, and the owned-view query in `collection_store.views`, which the presentation overlay already used. It adds no dependency, storage, vocabulary or governance rule. On the sandbox copy, records list moved from 2,095 ms to 455 ms p50 across three adjacent base/head pairs at the MCP boundary, and the sweep reported the same three legacy trackers with and without the skip.
