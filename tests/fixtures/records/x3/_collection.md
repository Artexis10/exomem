---
type: collection
exomem_id: 9ba8d1cf-d1e7-4309-95ae-cb28d7a6eea8
title: X3 training sessions
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-log
  source: Training Log.md
  format_version: 1
  heading:
    level: 2
    date_format: "%Y-%m-%d"
  section: Sessions (newest first)
  insertion: newest-first
  archive: Historical Reps (undated).md
  child_rows:
    delimiter: "|"
    fields: [movement, band, repetitions]
item_schema:
  natural_key: [occurred_on, title]
  fields:
    occurred_on:
      type: date
      required: true
    title:
      type: string
      required: true
    status:
      type: enum
      enum: [completed, partial, aborted]
    movements:
      type: array
      items:
        type: object
templates:
  - path: Push.md
  - path: Pull.md
links:
  plans:
    - reference: exomem://memory/81947000-4c22-46e4-9874-23fed028314b
      query:
        filters:
          status: completed
        limit: 24
---

A human-owned X3 training-log contract. The log remains ordinary Markdown.
