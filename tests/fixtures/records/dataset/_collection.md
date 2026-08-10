---
type: collection
exomem_id: 922bc7b2-2199-4f5b-b7a0-94f316fbf589
title: Meter readings
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: dataset
  source: readings.csv
  format_version: 1
  key: reading_id
item_schema:
  natural_key: [reading_id]
  fields:
    reading_id:
      type: string
      required: true
    occurred_on:
      type: date
      required: true
    category:
      type: string
      required: true
    value:
      type: number
      required: true
---

Query-only CSV source. Append and update are intentionally unsupported.
