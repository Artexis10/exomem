---
type: collection
exomem_id: 49622075-9ff4-4660-9ab7-414854b5bca2
title: Vehicle maintenance
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Events
  format_version: 1
item_schema:
  natural_key: [occurred_on, asset, provider]
  fields:
    occurred_on:
      type: date
      required: true
    asset:
      type: link
      required: true
    odometer:
      type: integer
    provider:
      type: string
    services:
      type: array
      items:
        type: string
    amount:
      type: number
    currency:
      type: enum
      enum: [GBP, EUR]
    status:
      type: enum
      enum: [scheduled, completed]
    receipt:
      type: link
    next_due_on:
      type: date
    next_due_odometer:
      type: integer
---

One ordinary Markdown file per maintenance event.
