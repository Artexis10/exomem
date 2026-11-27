# Serve a named page's Records sums in recall

## Why

A person keeps a running tally in Records: what a friend owes, shared costs, loans. When the user asks "what does this person owe me", recall finds the person's page and can count the rows that cite it (`cited_by_count`). It cannot say what those rows add up to. The agent then reads the collection row by row, or answers from a stale note.

On 2026-11-27 a turn named a person with a running tally. The activation spent its time budget on task discovery, so the task context came back partial and carried neither the collection's method nor its rows. The agent answered without the tally.

The pieces exist:

- the turn slice resolves the named page as a lead;
- `page_citations` admits collections, link fields and rows under the caller's view, and its index finds the rows that cite a page;
- collection `claims` decide whether a turn touches a collection.

What is missing is a declaration of how a collection's rows add up, and a block that serves the sums beside the lead.

## What Changes

- **Manifest `subject`.** A Records collection may declare `subject`:
  - `field`: the link field that names the page;
  - `measure`: the number to sum;
  - `state`: an optional field that splits the sums;
  - `order`: an optional date that ranks the newest rows;
  - `show`: up to four fields for each newest row;
  - `recent`: 0 to 10 newest rows, 3 by default.

  A collection create, revise and validate refuse a malformed declaration and name what to change; a stored manifest that used `subject` for something else still reads, with no ledger. A field may carry a `description`, which a reader shows beside the value, such as which sign of an amount means what.
- **`subject_records` in `recall_context`.** When the turn names a page by its title or alias, rows cite that page through `subject.field`, and the turn holds a claim term of the collection, the packet carries one entry for the pair. Each entry leads with a sentence that the agent can repeat. It has the sums by state and in total, the counts of rows with no amount or no date, the newest rows and the collection's method page. The block sits at the top level of the packet, so it survives a starved task context.
- **Nothing dropped in silence.** At most three entries carry numbers. A later pair, or one that the packet has no room for, stays as an `omitted` stub, and the block counts every pair it left out.
- **Honest unknowns.** A missing amount is counted as unknown and never summed as 0. A building index reads `warming`. A withheld field reads `unavailable`. Neither carries a number, and neither entry appears unless the caller may read a row that cites the page.
- **A failure is visible.** When the block cannot be read, or a ledger's index is still building, the block itself reads `unavailable` or `warming`. That state names no page, and it depends only on the vault and the turn.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `records`: a Records collection declares how a page's rows add up.
- `context-activation`: a served packet carries the sums of a named page's rows.

## Impact

- `structured_collections.py` parses `subject` and the field `description`.
- A new module, `subject_records.py`, reads the sums. `page_citations.py` exposes the cited-row join that it already applies. `NativeLayout` names a field's stored cell.
- `commands.py` attaches the block before the lead citations. A lead whose `ready` entry carries newest rows keeps its citation count, and its list holds only other collections' citations. `page_citations.for_hits` counts a field's citations without listing them for this.
- The skill scaffold teaches the declaration. One user note announces the change.
- The change adds no store schema, no write path and no change to method activation or `next_action`.
