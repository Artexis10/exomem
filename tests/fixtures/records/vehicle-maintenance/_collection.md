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
  natural_key: [occurred_on, service]
  fields:
    occurred_on:
      type: date
      required: true
    service:
      type: string
      required: true
    mileage:
      type: integer
    due_mileage:
      type: integer
    amount:
      type: number
    currency:
      type: string
    evidence:
      type: array
      items:
        type: link
---

One ordinary Markdown file per maintenance event.
