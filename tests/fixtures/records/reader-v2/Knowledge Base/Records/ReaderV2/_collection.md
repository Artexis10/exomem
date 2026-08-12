---
type: collection
exomem_id: 55c52de4-d3b7-466a-b099-3e28d1e64e8a
title: Reader v2 lifecycle fixture revised
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Items
  format_version: 1
item_schema:
  natural_key: [occurred_on, label]
  fields:
    occurred_on:
      type: date
      required: true
    label:
      type: string
      required: true
    status:
      type: enum
      enum: [open, complete]
views:
  latest:
    sort: [occurred_on, desc]
record_audit: {version: 2, head: cdef012345abcdef012345ab}
---

Frozen reader-v2 fixture.
