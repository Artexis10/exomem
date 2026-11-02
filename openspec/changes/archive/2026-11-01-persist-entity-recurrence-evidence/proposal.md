# Proposal

## Why

An explicit `entity_recurrence` attention call re-reads and re-parses every Knowledge Base page and re-derives every candidate. Shipped guidance makes each agent session run this call once, and `entity_candidate_by_ref` runs it again when a curation work item opens. On a large vault the call takes tens of seconds to minutes. Most of that time is reading pages that have not changed since the previous call.

## What Changes

- Persist per-page recurring-identity evidence in a derived store under the vault state directory. Key each row by the page's file identity.
- On each explicit entity-recurrence sweep, walk the Knowledge Base by file metadata, extract evidence again only for pages whose identity changed, and drop rows for pages that are gone.
- Apply the audience's egress checks to every walked page before its row is read. Then run the cross-page aggregation over visible pages only, so the findings are byte-identical to a full sweep.
- Recompute the frames that depend on registry anchors on every call from stored relation clauses, so a new or re-aliased Entity needs no file reads.
- Keep every other audit composition on its current path.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `action-first-audit`: recurring-identity evidence may persist between sweeps without changing any finding.

## Impact

The change affects `entity_recurrence`, the audit sweep for that category, and one new derived store. It adds no write-time work, no public schema, and no change to findings, fingerprints, review identity, or ordering.
