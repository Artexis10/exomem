---
status: inactive-example
---

# Optional context incident collection

This is an inactive schema example, not a collection installed in the vault.
Reuse a compatible existing incident collection and its returned identity.
The normal collection proposal and confirmation procedure still applies when no suitable collection exists.
The example identifier and field names describe example data, not an existing vault reference or required product vocabulary.

```yaml
---
type: collection
exomem_id: 386b1eed-8a5f-4b8d-a5a0-6fd6557c5705
title: Context incidents
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Incidents
  format_version: 1
item_schema:
  natural_key: [occurred_on, label]
  fields:
    occurred_on: {type: date, required: true}
    label: {type: string, required: true}
    request_ref: {type: link}
    source_evidence: {type: array, items: {type: link}}
    available_context: {type: array, items: {type: link}}
    proposed_correction: {type: string}
    target_ref: {type: link}
    write_receipts: {type: array, items: {type: link}}
    retrieval_check: {type: link}
    later_answer: {type: link}
    open_question: {type: string}
---
```

Inspect the actual manifest before writing. Its natural key defines identity; reuse the incident reference for later corrections and checks.
Use only actual returned source, target, receipt, and verification references.
Missing links remain missing evidence, not successful checks.
An uncertain write result cannot fill the role of a terminal receipt.

Different collections can declare different names for these roles:

| Evidence role | This example | Another declared schema could use |
| --- | --- | --- |
| Source-backed expectation | `source_evidence` | `basis` |
| Context that reached the agent | `available_context` | `observed_input` |
| Committed correction evidence | `write_receipts` | `change_proof` |
| Retrieval and ordinary-answer checks | `retrieval_check`, `later_answer` | `checks` |
| Unresolved evidence or behavior | `open_question` | `remaining_work` |

Read the existing schema and map these meanings with the agent's judgment.
Do not rename its fields, invent lifecycle values, or treat these illustrative alternatives as aliases the product recognizes.
The [learning workflow](../../references/context-learning.md) supplies diagnosis, authority, and closure rules.
