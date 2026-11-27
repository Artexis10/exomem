## Why

An owner keeps identity documents, government ID numbers, health records and financial statements in Evermatter. Agents treat such material as something to protect by leaving it out:

- They refuse the capture.
- They cover the value.
- They keep only a pointer to where the real document lives.

The shipped instructions encouraged this. One rule refused capture of "unrelated PII". On 2026-11-25 an agent declined to keep a user's identity-card photos under it, and a later form fill had nothing to read. The skill rebuild of 2026-11-27 removed that rule, but nothing yet tells an agent to keep such material, and a bare "never keep a card number" beside an ID card still reads as a rule about ID-card numbers.

Agents also have no way to say that a page is sensitive. So nothing later can restrict who sees the page, and omission stays the agent's only tool.

The owner's model is different. Keep everything, label it, and restrict disclosure through governance when needed. This change delivers the first half of that model:

- Material is captured in full.
- A sensitivity class is recorded as metadata.
- The class changes at any time without touching the captured bytes.

Restriction by class comes in later changes. Inheritance along citations and withheld-equals-missing on every read door come first. Per-audience policy comes after them.

## What Changes

- **A `sensitivity-classes` vocabulary registry.**
  - A shipped pack and a vault overlay hold the class keys, so code holds no class list.
  - A class is a label and carries no behaviour. Who reads a page of a class is a governance policy decision: a later change lets a policy scope select pages by class, and its existing `default_deny` expresses "the owner only".
  - The shipped classes are fixed; a vault overlay only adds one.
- **A `sensitivity` frontmatter field.**
  - The field holds a list of class keys and is declared in the `page-fields` pack with `role: pointer`, `shape: list` and a new `writer: classify`.
  - On an append-only page (Sources, Evidence), only the classify operation changes it.
  - That operation goes through the governed pointer write. Every other byte stays identical, the file never moves, and a reason record allows a revert.
- **Capture takes a class.**
  - `save_source`, `save_evidence`, `create_note`, `save_episode` and `add_record` accept an optional `sensitivity` list, which is the calling agent's judgement.
  - A Source kind may declare a default class, which applies when a capture declares none. A default that names no registered class is skipped and never refuses the capture.
  - Each capture result reports the class applied, its basis and the current disclosure policy for it.
- **Classify any time.** `manage_memory(operation="files.classify")` sets or clears the class of one page, with a required reason, and can preview the change. A plain edit of `sensitivity` on a compiled page accepts only registered keys as a list. Collections take a collection-level class as a floor, and a row may add its own where its schema declares a `sensitivity` field.
- **Instructions stop asking for omission.**
  - Agents keep identity, health and financial material in full.
  - Agents classify it, and ask the owner only when a disclosure decision is genuinely unclear.
  - Authenticators (payment card numbers, security codes, passwords and live credentials) stay excluded by default, as the server's capture redaction already does. An owner-level opt-in to keep them belongs to the per-audience change, because a kept authenticator is safe only once a restriction can hold it back.

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
- Disclosure: this change adds no restriction, and it widens what is stored. Agents now keep identity, health and financial material that they used to refuse, in every edition. Until the per-audience change ships, every owner client, recall injection, search, backups and, on a hosted cell, the operator can read it. Each capture result says so, and the owner can classify now so that a later policy restricts by class at once.
