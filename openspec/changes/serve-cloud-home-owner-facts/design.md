# Design

## Context

See `proposal.md` for the gaps.
Home reaches a cell only through the Cloud gateway and the cell's stateless `/mcp` endpoint.
The gateway authenticates the owner, derives the cell bearer and forwards an allowlist of request headers.
A cloud cell resolves that bearer to the cell owner.

## Goals / Non-Goals

Serve the facts Home needs as MCP output fields, with unknown values as null.
Add no route, no model and no write path. Keep every new field free of content.
The gateway's own change and the Home page are out of scope.

## Decisions

### One precedence rule

`prominence.resolve_context(context)` holds the only precedence rule: environment, the context's saved value, the identity-wide saved value, machine config, the unreadable-record floor, then the context default.
`resolve` and the active-source report both answer through it, so the level a client gets and the level Home shows cannot disagree.
The context is a parameter, so one request can report both contexts. It still tunes eagerness only.
Setting and clearing one context, with the revision compare-and-set, already exist in `configure_memory`; this change reuses them.

### Released items come from a release set, not the receipt key

Measured on a cloud cell with the default empty policy: `ask_memory` returned five notes while the disclosure collector's `path_outcomes` stayed empty.
That set is the receipt's dedupe key, and the empty-policy shortcut releases without a receipt.
The collector therefore keeps a separate set of released identities, filled wherever a release decision is made, whatever the policy.
The release decisions are the receipt schema's closed set (`released`, `release_authorized`).
The outermost read boundary of a call reports the size of that set.

### Access and write targets

`access` is the classification the dispatcher already made for the call, carried to the ledger row through the call token, like committed targets.
A write's `item_count` is the number of distinct pages the commit reported.
At the commit, the dispatcher reports the pages that the committed terminal names (`path` or `paths`), so a write whose destination the server chose, such as `remember`, still has a count.
Only when the commit reported none does the count fall back to the distinct targets the arguments name.
The two are not added: an argument can name a committed page by its `exomem://` reference, and the union would count that page twice.
Targets are compared whole, before the row bounds the paths it shows.
A write whose arguments name no page and whose terminal names none has no known target, so its count is null.
A call that did not succeed has a null count.

### Activity

The action comes from the command registry's `product_actions` and the row's `access`: a read that asks is `recalled`; a write that saves, records or updates is `saved`.
Rows of one client family and action join the newest event of that pair when they are at most 60 seconds older than its newest row.
An event's `items` is null when any joined row's count is null.
`since` is the oldest row the returned list accounts for, or the ledger writer's start when there is none.
The reader stops at the first row the list cannot represent, so `since` never covers an omitted row:
- a qualifying call that the full list has no room for;
- a damaged row: not JSON, no valid time, a `row_hash` that `call_ledger.row_hash` does not reproduce, or a `sequence` and `prev_hash` that do not link to the newer row, which shows a removed row;
- a version 1 row, which cannot say whether it read or wrote.
The ledger appends rows in time order, so every row that the reader did not read is no later than a boundary time:
- the stopping row's own time, when that row is intact;
- the oldest time read, when the stopping row is damaged or rotation archived the older rows.
Rows can share a millisecond, the API's precision, so the list accounts only for rows strictly later than the boundary.
This drops the whole millisecond group that can hold the damage.
The reader counts the events again over the rows it keeps, so a dropped row adds nothing to a count.
When no kept row remains, `since` is the writer's start if the boundary is earlier than that start.
Otherwise the section is `ACTIVITY_UNAVAILABLE`, as for a damaged newest row or a version 1 row from an older process that still writes.
Measured on a laptop, a verified scan of an 8 MB live file with no qualifying call takes 114 ms (median of five).
The reader skips the text after the file's last newline, which is a row still being appended.
A failed append writes no row and leaves no gap in the sequence, so the file cannot show the missing call.
The failing process therefore records the time of each failed append in memory, and best effort in a marker file beside the ledger (`ledger.gap`).
The reader treats the later of the two times as a boundary: the interval starts strictly after the failed call.
With no row after that time, the section is `ACTIVITY_UNAVAILABLE` until the next call that the ledger records.
The memory call itself still succeeds.
On a Cloud cell one MCP server process serves the recalls, the saves and the activity section, so its memory covers every call.
The marker covers a reader in another process, such as a second server beside a local service.
An unreadable marker makes the section `ACTIVITY_UNAVAILABLE`, because the reader cannot know whether a call is missing.
The section is owner-only: it shows when and how much the owner's other assistants used memory.
A disabled or unreadable ledger is the error `ACTIVITY_UNAVAILABLE`, never an empty list.

### Note count

The owner chose the meaning: the count holds everything saved, not compiled notes only.
It counts every page that the Knowledge Base corpus walk yields: sources, records kept as pages, entities, evidence and compiled notes.
Navigation pages (`index.md`, `log.md`) and Records collection manifests (`_collection.md`) are structure, not saved items.
Rows of a record dataset kept as one CSV are not pages, and are not counted.
The count is null when the walk cannot read any directory or entry.
The cell walks the corpus on every activity call and keeps no cached count.
A cache needs a token that every writer advances, and none exists:
- the commit generation on disk stays unchanged when its write fails after a committed save;
- a counter in one process cannot see a commit by another writer process, such as the media worker.
Measured on a laptop with a warm file cache, one walk takes 44 ms for 1,000 pages, 410 ms for 10,000 and 1,221 ms for 30,000 (median of five).

### The gateway's client header

Measured with a real stateless request through a cloud-mode server: a handshake-protocol `tools/call` with the gateway's `User-Agent` resolved to surface null and the coding context.
The same call with a client-naming `User-Agent`, or with a per-request `clientInfo` from the 2026-07-28 protocol, resolved to the conversation context.
On a cloud cell, the caller's client name reads `x-exomem-oauth-client` after `clientInfo` and before `User-Agent`.
Only a cloud cell trusts it: there the cell bearer proves the gateway sent the request, and the gateway forwards no caller copy of the header.

### The registry decides a Cloud client's context

Surface detection matches only some client names, so the web could know an app's context only by copying that name list.
On Cloud, the control plane's client registry decides instead: it derives each client's context from data it already holds, and one function serves both the gateway and the web.
The gateway sends that context in `x-exomem-client-context`, next to the client name.
A cloud cell honours `coding` or `conversation` there, after the operator's surface override and before name detection.
It ignores any other value, because the header tunes eagerness only and detection still has an answer.
Home then saves a level to exactly the context the cell applies to that app, before the app's first call.

## Risks / Trade-offs

A local install still depends on surface detection, which matches only some client names.
If the marker write fails as well, for example on a full or read-only volume, only the failing process knows about the lost call.
A reader in a different process can then claim an interval that holds that call.
On a Cloud cell this needs a second process that records calls, and the cell runs only one.
The ledger does not sync each row, so a power loss can drop rows that the kernel had not yet written.
The next process continues the chain from the last surviving row, and the activity list cannot see the lost calls.
