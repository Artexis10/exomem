## Why

An owner keeps identity documents, government ID numbers, health records and financial statements in Evermatter. Agents treat such material as something to protect by leaving it out:

- They refuse the capture.
- They cover the value.
- They keep only a pointer to where the real document lives.

The shipped instructions encourage this. One rule refuses capture of "unrelated PII". Another rule ("never keep a card number") reads to an agent as a rule about ID-card numbers.

Agents also have no way to say that a page is sensitive. So nothing later can restrict who sees the page, and omission stays the agent's only tool.

The owner's model is different. Keep everything, label it, and restrict disclosure through governance when needed. This change delivers the first half of that model:

- Material is captured in full.
- A sensitivity class is recorded as metadata.
- The class changes at any time without touching the captured bytes.

Restriction by class comes in later changes. Inheritance along citations and withheld-equals-missing on every read door come first. Per-audience policy comes after them.

## What Changes

- **A `sensitivity-classes` vocabulary registry.**
  - A shipped pack and a vault overlay hold the class keys, so code holds no class list.
  - Each entry declares `default_disclosure`, a closed attribute whose values are `owner-only` and `vault-default`. This change records the attribute. A later change enforces it.
- **A `sensitivity` frontmatter field.**
  - The field holds a list of class keys and is declared in the `page-fields` pack with `role: pointer`, `shape: list` and a new `writer: classify`.
  - On an append-only page (Sources, Evidence), only the classify operation changes it.
  - That operation goes through the governed pointer write. Every other byte stays identical, the file never moves, and a reason record allows a revert.
- **Capture takes a class.**
  - `save_source`, `save_evidence`, `create_note`, `save_episode` and `add_record` accept an optional `sensitivity` list, which is the calling agent's judgement.
  - A Source kind may declare a default class. Agent-less captures use the default.
  - Each capture result reports the class applied, its basis and the current disclosure policy for it.
- **Classify any time.** `manage_memory(operation="files.classify")` sets or clears the class of one page, with a required reason, and can preview the change. Collections take a collection-level class, and a row may carry its own.
- **Instructions stop asking for omission.**
  - Agents keep identity, health and financial material in full.
  - Agents classify it, and ask the owner only when a disclosure decision is genuinely unclear.
  - Authenticators (payment card numbers, security codes, passwords and live credentials) stay excluded.

## Capabilities

### New Capabilities

- `sensitivity-classification`: the class registry, the class field, classification at capture, the classify operation, the reporting of class and policy, and the agent capture contract for sensitive material.

### Modified Capabilities

- `page-field-registry`: the shipped pack declares `sensitivity` as a list pointer field, and `writer` gains the value `classify`.

## Impact

- Code:
  - `vocabulary/` (a new registry module and pack)
  - `page_fields.py` (the `classify` writer)
  - the `source-kinds` registry (an optional `sensitivity` attribute)
  - the capture commands and `structured_collections` / `collection_store` governance metadata
  - a classify operation under `manage_memory`
  - capture result shapes
- Instructions: the scaffold skill references, the `evermatter-capture` workflow skill, the generated plugin copies, the upload page, and the activation learning instruction with its guide copies.
- Data:
  - No existing page changes until an owner or agent classifies it. A page without `sensitivity` reads as unclassified.
  - Captured Source and Evidence bytes are never rewritten.
  - A class edit is a frontmatter-span change through the governed pointer write.
- Disclosure: none in this change. With no policy, every client signed in as the owner reads a classified page as before.
