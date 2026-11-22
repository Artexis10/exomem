---
exomem_id: 8cbda207-0bac-546e-bdef-3c4c1bda9562
type: collection
title: Memory Incidents
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: sqlite
  source: Incidents
  format_version: 1
item_schema:
  natural_key: [incident_key]
  fields:
    incident_key:
      type: string
      required: true
    observed_on:
      type: date
      required: true
    surface:
      type: enum
      required: true
      enum: [chat, recall, save, search, import, other]
    symptom:
      type: string
      required: true
    expected:
      type: string
    actual:
      type: string
    severity:
      type: enum
      enum: [low, medium, high]
    status:
      type: enum
      required: true
      enum: [open, diagnosed, fixed, wont_fix, duplicate]
item_filename:
  version: 1
  fields: [incident_key]
item_presentation:
  version: 1
  title: incident_key
  summary: [status, severity, surface, observed_on]
---

# Memory Incidents

One item stands for one concrete failure of the assistant's memory: something I had it keep that it did not keep, kept in the wrong place, never saved when it should have, or gave back stale or wrong. The collection is a ledger of what happened, so that patterns show up and fixes can be checked against the original occurrence.

## Rules

- Write the concrete occurrence first: when it was seen, what I asked or said, what I expected and what came back. Use my words where they fit.
- Diagnose afterwards. A cause is a later update to the same item, never a reason to delay the item.
- One item per occurrence. Never fold several incidents into one, even when they look alike. Link a repeat to the earlier item with `status: duplicate` only when it is the same occurrence reported twice.
- `surface` says where the failure showed: in conversation, when recalling, when saving, in search, or on import.
- `severity` is mine to judge. When I do not say, leave it empty and do not guess.
- A fixed incident keeps its item. The history is the point.

## What does not belong here

Ordinary corrections to a fact, a changed mind, and requests for new features are not incidents. A fact that was wrong when saved belongs to the page that holds it, not to this ledger, unless the saving itself failed.
