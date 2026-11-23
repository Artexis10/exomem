---
exomem_id: 937500cf-18dc-5de9-8e3f-c4aa2c4b4d57
type: collection
title: Engineering Experiments
semantic_profile: planning
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: sqlite
  source: Items
  format_version: 1
item_schema:
  natural_key: [title]
  fields:
    title:
      type: string
      required: true
    kind:
      type: string
    status:
      type: string
    lifecycle:
      type: string
    priority:
      type: string
    commitment:
      type: string
    horizon:
      type: string
    health:
      type: string
    area:
      type: string
    parent:
      type: string
    pointer:
      type: link
item_filename:
  version: 1
  fields: [title]
item_presentation:
  version: 1
  title: title
  summary: [kind, status, lifecycle, priority, commitment, horizon, pointer]
---

# Engineering Experiments

The intended work behind my side projects: scoped trials of tools and techniques, and the small jobs that follow from them.

## Rules

- One item per trial or job, titled so it reads on its own.
- A trial item states its scope, its time box and what result would count as success.
- A trial is committed only after I say go. Until then it is an idea and carries no commitment.
- The result of a trial goes to Technology Signals as outcome evidence. This collection holds the intent, not the verdict.
