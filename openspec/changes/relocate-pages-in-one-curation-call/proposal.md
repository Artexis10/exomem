## Why

Moving a set of pages into another project, or renaming them to their titles, takes one curation plan. Today the agent must write that plan by hand, and the lane cannot carry it at the size such a repair needs. On 2026-11-22 a rehome on the personal service showed four failures:

- A 61-step plan sealed at 1.19 MB, and apply then refused it as corrupt, because the read bound was smaller than what the seal accepted.
- Two proposals ran past the client's 300 s timeout and never sealed, with no error.
- 47 Source edits took 47 apply or resume calls, because each request ran one step.
- Writer-busy refusals rose about six times while the moves ran, because apply and resume held the vault write boundary for the whole request.

An agent-facing interface should be one obvious operation. A rehome of many pages is one decision for the person, so it should be one call, one preview and one approval, and one approval should undo it.

## What Changes

- A new curation action `relocate` takes `pages` or one `review_ref`, and optionally `to_project`, `folder` and `from_project`. The server derives the whole plan read-only: a move or an in-place field edit of each page, an edit of the project fields of each cited Source, and the closing `repoint-links` steps, split by referring page into bounded steps when there are many. It seals one plan and answers with a grouped summary.
- A cited Source gains the destination key. It loses the replaced key only when no page outside the set still cites it under that key.
- `apply` and `resume` run as many steps as the request budget allows. Each step holds the vault mutation boundary only at its own seams, so a foreground write lands between steps. A request that stops early answers `executing` with `progress`. A refusal after committed steps answers `stopped` beside the progress instead of raising.
- A derived plan carries a sealed `derived_by` field. Its step cap is 2048, and one 1 MiB bound applies at seal and at every read. A plan above it is refused at propose, before anything is written. Agent-written plans keep 64 steps and 256 KiB.
- Steps whose undo does not read the page's full text (move, pointer edit, repoint, delete, recover) seal no preimage.
- The preview, the relocation reply and the undo proposal of a derived plan are grouped summaries with counts and samples, never the step list.
- A curation `edit` that patches one pointer field without an owning operation takes the governed pointer write on any page outside a curated tree, so entities and other pages that `replace_memory` cannot re-create can be rehomed and undone.
- A new closed page-field attribute `carries: project-key` names the fields that hold project keys. It replaces the field names that the pointer write held in code.
- A new closed page-field attribute `shape: list` names the fields whose first value is written as a list.

## Capabilities

### Modified Capabilities

- `governed-curation-lane`: relocation derivation, multi-step execution per request and its stop after committed steps, derived-plan size bounds, summary previews, the ownership question for derived plans.
- `command-surface`: the `relocate` curation action; the pointer write for any page outside a curated tree.
- `page-field-registry`: the `carries` and `shape` attributes.

## Impact

- Code: `src/exomem/relocate.py` (new), `curation.py`, `commands.py`, `operation_entries.py`, `mutation_terminal.py`, `set_frontmatter_field.py`, `page_fields.py`, `note.py`, `writer_lease.py`, the `page-fields` pack.
- Surfaces: the MCP schema fixture, the discovery fingerprint and the skill text that describes curation execution.
- Data: stored plans gain an optional `derived_by` field. Plans sealed before this change load unchanged.
